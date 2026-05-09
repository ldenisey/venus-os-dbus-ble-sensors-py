# BLE Advertisement Router — Consumer Guide

This document is for service authors writing **external** Venus OS services
that want to consume BLE advertisements without building their own scanner.
End users and operators wanting to install or configure this service should
see [`README.md`](../README.md).  Contributors adding new internal device
classes inside `dbus-ble-sensors-py` itself should see
[`DEVELOPMENT.md`](../DEVELOPMENT.md) — those don't need the router and get
advertisements directly via the device-class autoloader.

---

## What this is

The advertisement router lets you build a Bluetooth integration **without
writing any BLE scanning code**.  You declare which advertisements your
service cares about — by manufacturer ID, product ID, MAC address, or any
combination — and receive them as D-Bus signals.  HCI scanning, AD-structure
parsing, deduplication, multi-adapter handling, and contention with other
BLE services are already solved.  This service does it once, on behalf of
every consumer on the system.

The router is **part of `dbus-ble-sensors-py`**, which is the official
Victron service backing the Cerbo "Bluetooth Sensors" UI.  By plugging in
here, your integration sits where Venus OS already expects to look for
Bluetooth devices, and can leverage the existing discovery and management
surfaces (see [Participating in the Victron UI](#participating-in-the-victron-ui)
below).

## Why use it (instead of writing your own scanner)

- **BlueZ allows only one active scanner per adapter.** If
  `dbus-ble-sensors-py` is already running on a Cerbo (it's the default),
  any service that calls `StartDiscovery` or runs Bleak in active mode will
  fight it for the adapter and one side loses.  The router lets you both
  win: `dbus-ble-sensors-py` does the scanning once, and routes to you.
- **Passive HCI scanning is non-trivial.**  Doing this correctly means a
  raw `HCI_CHANNEL_MONITOR` socket, parsing HCI frame headers, walking
  AD structures, decoding manufacturer data, deduping identical packets,
  and tracking which adapter heard what.  See
  [`hci-tap-architecture.md`](hci-tap-architecture.md) for the full
  pipeline.  Re-implementing this for every integration is hundreds of
  lines of low-level Linux Bluetooth code that you'd have to maintain.
  Subscribing to a D-Bus signal is ~10 lines.
- **What you get for free:** RSSI, adapter attribution (which `hciN`
  heard the ad), per-MAC dedup keepalive (you still see a refresh every
  ~15 minutes even if the data didn't change), automatic mfg-filter
  widening so the tap actually picks up your traffic, lifecycle cleanup
  via `NameOwnerChanged` (your service crashes — your registrations
  unregister themselves).

## What you do with the advertisement is up to you

The router is a **trigger and data feed**.  Once an advertisement reaches
your service you can do anything: decode the manufacturer payload, log
it, repackage it onto another D-Bus path, or use it as a presence signal
to open a GATT connection in your own code.  The router is not an
alternative to GATT — a common pattern is "subscribe to advertisements
to detect presence and read passive data, then open a GATT connection
when you need read/write or notifications."  Use Bleak or dbus-fast for
GATT alongside the router.

## When *not* to use the router

- You're an internal `BleDevice` subclass inside `dbus-ble-sensors-py`
  itself — the autoloader already gives you advertisements directly.
- You need raw HCI traffic the tap doesn't surface (non-LE events, scan
  responses, custom AD types not parsed by `_walk_ad_structures`) — open
  your own `HCI_CHANNEL_MONITOR` socket.

---

## Service surface

| | |
|---|---|
| Bus name | `com.victronenergy.switch.ble_advertisements` |
| Root path | `/ble_advertisements` |
| Interface | `com.victronenergy.switch.ble_advertisements` |
| Signal signature | `sqaynss` (see below) |

### Methods on the root path

```
GetVersion()   -> s   # service version, e.g. "1.1.1"
GetStatus()    -> s   # "running" if heartbeat fresh, else "stale"
GetHeartbeat() -> d   # unix timestamp of last advertisement processed
```

The heartbeat is bumped on every advertisement that flows through
`_process_advertisement` (whether or not it matches a registration), so
on a system with active BLE traffic it stays current within a few
seconds.  `GetStatus` returns `"stale"` if no advertisement has been
processed for 30 minutes — useful for a liveness gate before your
service starts.

### `Advertisement` signal

```
Advertisement(s mac,
              q manufacturer_id,
              ay data,
              n rssi,
              s interface,
              s name)
```

| Field | Type | Notes |
|---|---|---|
| `mac` | `s` | Uppercase, colon-separated, e.g. `"AA:BB:CC:DD:EE:FF"` |
| `manufacturer_id` | `q` | uint16 BLE company ID (e.g. `0x02E1` for Victron) |
| `data` | `ay` | Manufacturer-specific payload, with the company-ID prefix already stripped |
| `rssi` | `n` | int16 dBm; `0` if the tap couldn't determine it |
| `interface` | `s` | `"hciN"` of the adapter that heard the ad |
| `name` | `s` | Currently always empty.  Populating it requires the tap to parse AD types `0x08`/`0x09` (shortened/complete local name); not implemented yet |

The signal is emitted on the **registration path**, not on the root
path.  Each consumer subscribes to the path it itself created — see the
next section.

---

## Registration paths

Your service registers interest by **creating empty introspectable
D-Bus objects** at well-known paths on its own bus name.  The router
finds them by walking introspection XML at startup (paced 100 ms per
service) and watches `NameOwnerChanged` for live add/remove afterwards.

There is no method to call; **the existence of the object at the path
is the registration.**  When the object goes away (you remove it, or
your service exits), the router cleans up.

### Path patterns

| Path | When to use |
|---|---|
| `/ble_advertisements/{service_label}/mfgr/{decimal_id}` | Every advertisement from a manufacturer |
| `/ble_advertisements/{service_label}/mfgr_product/{mfg}_{pid}` | One specific product within a manufacturer |
| `/ble_advertisements/{service_label}/mfgr_product_range/{mfg}_{lo}_{hi}` | A contiguous product-ID range |
| `/ble_advertisements/{service_label}/addr/{MAC_NO_COLONS}` | One specific device |

**Notes on the path components:**

- `{service_label}` is a token of your choosing — typically your
  service's short name, with underscores instead of hyphens (D-Bus paths
  forbid hyphens).  It should also be a substring of the bus name your
  service owns; the router uses bus-name-in-path as a substring-match
  hint when cleaning up registrations after `NameOwnerChanged`
  disappear.  In practice: pick a label that appears in your bus name.
  Example: bus name `com.victronenergy.switch.my_thermo`, label
  `my_thermo`.
- `{decimal_id}`, `{mfg}`, `{pid}`, `{lo}`, `{hi}` are decimal integers
  (Victron's company ID `0x02E1` is `737` in decimal).
- `{MAC_NO_COLONS}` is twelve hex characters, uppercase, no separators
  (`AABBCCDDEEFF`).  Mixed-case and underscore-separated forms are also
  recognised by the parser.

### Product-ID semantics

Product IDs apply to manufacturers that put a uint16 product ID at
bytes 2–3 (little-endian) of their advertisement payload.  Victron does
this for every Instant Readout advertisement.  See the `BleDevice`
subclasses in this service (`ble_device_*.py`) for examples per
product family, and the vendored `victron-ble` package
(`ext/victron_ble/`) for the encrypted-payload decoder.

If a manufacturer does *not* follow this convention, use plain `mfgr/`
registrations and parse product identity yourself from the data field
in your signal handler.

### What gets emitted to whom

For each incoming advertisement, the router emits the `Advertisement`
signal **once per matching registration path**.  If your service has
two paths that both match (e.g. both an `mfgr/737` and an
`addr/AABBCCDDEEFF`), it gets the signal twice — once on each path.
Subscribe to whichever paths give you the granularity you want and
ignore overlap server-side.

---

## Minimal walkthrough

A complete consumer service is roughly 30 lines.  Skeleton:

```python
import dbus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

ROUTER_INTERFACE = 'com.victronenergy.switch.ble_advertisements'

class Empty(dbus.service.Object):
    """Empty introspectable object — its existence is the registration."""
    pass

class MyService:
    def __init__(self):
        DBusGMainLoop(set_as_default=True)
        self.bus = dbus.SystemBus()

        # Claim our own bus name so the router can introspect us.
        self.bus_name = dbus.service.BusName(
            'com.victronenergy.switch.my_thermo', self.bus)

        # Registration: every Victron 0x02E1 advertisement, on a path
        # whose label matches our bus name.
        self._registrations = [
            Empty(self.bus_name, '/ble_advertisements/my_thermo/mfgr/737'),
        ]

        # Subscribe to the same path the router will emit on.
        self.bus.add_signal_receiver(
            self._on_advertisement,
            signal_name='Advertisement',
            dbus_interface=ROUTER_INTERFACE,
            path='/ble_advertisements/my_thermo/mfgr/737',
        )

    def _on_advertisement(self, mac, mfg_id, data, rssi, iface, name):
        payload = bytes(data)
        # Do whatever you want with it.
        print(f"{mac} mfg=0x{int(mfg_id):04x} rssi={int(rssi)} "
              f"iface={iface} payload={payload.hex()}")

if __name__ == '__main__':
    MyService()
    GLib.MainLoop().run()
```

**Things to know about this skeleton:**

- The `Empty` class is a real `dbus.service.Object`, not a `pass`-only
  Python object.  It has to actually appear in introspection XML for
  the router to find it.  Keep references to your registration objects
  alive (`self._registrations = [...]`) — if Python garbage-collects
  them, they vanish from D-Bus and the router cleans them up on its
  next pass.
- The signal subscription path **must match** the registration path
  exactly.  Subscribing on the root path (`/ble_advertisements`) gets
  you nothing; the router doesn't emit there.
- All signal handling runs on the GLib main thread.  If your work is
  expensive, hand it off to a worker thread or queue rather than
  blocking the callback.

### Health-checking the router before you start

If your service requires the router (rather than treating it as
optional), gate startup on `GetVersion()`:

```python
def router_available(bus, timeout_ms=2000):
    try:
        dbus_iface = dbus.Interface(
            bus.get_object('org.freedesktop.DBus', '/org/freedesktop/DBus'),
            'org.freedesktop.DBus')
        if 'com.victronenergy.switch.ble_advertisements' not in dbus_iface.ListNames():
            return False
        root = bus.get_object('com.victronenergy.switch.ble_advertisements',
                              '/ble_advertisements')
        iface = dbus.Interface(root, 'com.victronenergy.switch.ble_advertisements')
        version = iface.GetVersion(timeout=timeout_ms / 1000)
        status = iface.GetStatus(timeout=timeout_ms / 1000)
        return status == 'running'
    except dbus.exceptions.DBusException:
        return False
```

`GetStatus() == 'running'` means the router has seen at least one
advertisement in the last 30 minutes.  On a Cerbo with any active BLE
device that's near-instant.

---

## Worked example: Victron Orion-TR family by product range

If your service handles every Orion-TR Smart variant, you don't need to
enumerate every product ID one at a time.  Use a `mfgr_product_range`
registration:

```python
# Orion-TR Smart product IDs span roughly 0xA381..0xA3D5
self._registrations = [
    Empty(self.bus_name,
          f'/ble_advertisements/my_orion/mfgr_product_range/737_{0xA381}_{0xA3D5}'),
]
```

The path's three components after `mfgr_product_range/` are decimal
integers — manufacturer, range low, range high, joined by underscores.
The router extracts the product ID from bytes 2–3 of the payload and
emits to your path only when it falls inside the range.

For decoding the encrypted payload that follows, see the vendored
`victron-ble` package at
`src/opt/victronenergy/dbus-ble-sensors-py/ext/victron_ble/`.

---

## Lifecycle and dynamics

- **Initial scan.**  At router startup, every service on the bus whose
  name begins with `com.victronenergy.` is introspected, paced one per
  100 ms, looking for paths under `/ble_advertisements/`.  Anything
  found becomes an active registration immediately.
- **Live add.**  When your service appears (claims its bus name), the
  router gets a `NameOwnerChanged` signal and re-introspects you within
  a few seconds.  No restart required.
- **Live remove.**  When your service exits, your name is dropped, the
  router cleans up your registrations and removes the per-path
  emitters.
- **Filter widening.**  The router collects all manufacturer IDs with
  active registrations and merges them into the HCI tap's manufacturer
  filter.  This means an unrecognised mfg ID — e.g. some new
  third-party sensor — only starts flowing through the pipeline after
  *some* service registers for it.  This is intentional: it keeps the
  GLib main thread off the hook for advertisements no one cares about.
- **Suppression unwinding.**  If the host service has previously
  ignored a MAC because no internal device class wanted it, and then
  your service registers for that MAC, the router clears it from the
  ignore list so subsequent advertisements reach you.  This applies
  only to MAC-level registrations; manufacturer-level registrations
  rely on the natural TTL expiry of the suppression cache (default
  10 min).

---

## Participating in the Victron UI

The standard Cerbo "Bluetooth Sensors" UI is backed by
`dbus-ble-sensors-py`'s **internal** device-class system.  Devices
recognised by an internal `BleDevice` subclass appear in the device
list automatically: the host service publishes a corresponding
`com.victronenergy.<role>.<adapter>_x<index>` D-Bus service, the GUI
discovers it, and the user gets a dedicated settings page.

**An external service consuming the router does *not* automatically
get a UI presence on its own.**  The advertisement signal flows to
your code; nothing about the UI changes until your service publishes
its own D-Bus device.

There are two paths to UI integration, depending on what you're
building:

### A) Publish your own `com.victronenergy.<role>.*` service

If your integration represents a discrete device with measurements
(temperature, tank level, switch state, …), the conventional Venus OS
pattern is to publish a service of the appropriate role.  The GUI
discovers it via the same mechanism it uses for everything else.

See:
- [Venus OS D-Bus API definition](https://github.com/victronenergy/venus/wiki/dbus-api)
  — naming conventions, mandatory `/Mgmt/*` paths, `/DeviceInstance`,
  `/ProductId`, `/ProductName`, the `/Devices/...` enumeration model.
- [List of available services and their paths](https://github.com/victronenergy/venus/wiki/dbus)
  — what each `com.victronenergy.<role>` path is expected to expose.
- [Switch service interface (`com.victronenergy.switch`)](https://github.com/victronenergy/venus/wiki/dbus_switch_s2)
  — if your integration exposes switchable outputs (the
  `/SwitchableOutput/<x>/...` pattern), this is the spec.

`velib_python`'s `VeDbusService` is the recommended helper; it's
available on Venus OS at `/opt/victronenergy/dbus-systemcalc-py/ext/velib_python/`
and is also vendored by `dbus-ble-sensors-py` itself.

### B) Add a new internal device class to `dbus-ble-sensors-py`

If your integration represents a Bluetooth device that fits the
"sensor with periodic advertisement" model already covered by the
internal device classes, the lowest-friction path may be to contribute
a `BleDevice` subclass directly.  See [`DEVELOPMENT.md`](../DEVELOPMENT.md)
for the conventions.  Internal classes get UI presence with no
additional D-Bus work on your side, but they live inside this service
and have to be merged here — which is fine for upstream-suitable work
and not fine for proprietary or experimental integrations.

The router (this document) is the right answer for everything that
doesn't belong inside `dbus-ble-sensors-py`.

---

## Things to know

- **Signals run on the GLib main thread.**  Don't block in
  `_on_advertisement`.  If decoding is non-trivial, push the work to a
  thread or async task.
- **Service-name ↔ path-component coupling.**  The router's cleanup
  path uses substring match between the bus name reported by
  `NameOwnerChanged` and the registered object paths.  Make sure your
  `{service_label}` is a substring of your bus name — otherwise
  cleanup on service exit may fail to remove your registrations.
  (This is a known limitation inherited from the standalone project;
  see the in-tree review notes.)
- **The `name` field is empty.**  The local-name AD type isn't parsed
  by the tap yet.  If you need a friendly name, you'll have to either
  cache it from a GATT exchange or wait for the tap to grow that
  capability.
- **Multi-adapter attribution is best-effort.**  The router reports
  the adapter index that the HCI tap saw the frame on (`hci0`, `hci1`,
  …).  This works correctly per-frame, but your service should not
  assume the same device will always be heard by the same adapter —
  on Cerbos with overlapping coverage, the same MAC can appear via
  different `hciN` from one ad to the next.

---

## Migrating from `dbus-ble-advertisements`

| Standalone capability | This router |
|---|---|
| Bus name `com.victronenergy.switch.ble_advertisements` | ✅ Same |
| Root path `/ble_advertisements` | ✅ Same |
| `GetVersion`/`GetStatus`/`GetHeartbeat` | ✅ Same |
| `mfgr/`, `mfgr_product/`, `mfgr_product_range/`, `addr/` registrations | ✅ Same |
| Signal `Advertisement` with signature `sqaynss` | ✅ Same |
| `NameOwnerChanged` lifecycle | ✅ Same |
| Initial introspection scan paced 100 ms per service | ✅ Same |
| Per-device deduplication keepalive | ✅ Inherited from host service (~15 min) |
| Per-device routing log throttle | ❌ Not present (host-service logging applies) |
| `/SwitchableOutput/relay_discovery` master toggle and discovered-device switches | ❌ Not present (Victron's existing Bluetooth Sensors UI applies) |
| Standalone process / install / disable scripts | ❌ Replaced by `dbus-ble-sensors-py` |

The signal contract and registration pattern are unchanged, so an
existing client written against the standalone should run against this
router without source changes — provided the standalone service is
disabled first to avoid bus-name contention.

---

## See also

- [`hci-tap-architecture.md`](hci-tap-architecture.md) — how
  advertisements physically reach this service from the kernel.
- [`README.md`](../README.md) — install, enable/disable, vendored
  dependencies.
- [`DEVELOPMENT.md`](../DEVELOPMENT.md) — contributing internal device
  classes (the alternative to using the router from outside).
