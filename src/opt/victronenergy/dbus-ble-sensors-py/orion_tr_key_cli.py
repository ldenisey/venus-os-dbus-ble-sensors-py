#!/usr/bin/env python3
"""Standalone Orion-TR advertisement-key fetcher.

Short-lived synchronous helper that the main service shells out to
whenever it needs to read VREG 0xEC65 from a paired Orion-TR.  Running
as a subprocess isolates the provisioning from any long-running process
state (bleak's BlueZ manager, dbus-python proxy cache, etc.) that we
have seen corrupt CCCD writes after repeated connect/disconnect cycles.

Usage::

    python3 orion_tr_key_cli.py MAC [--passkey N] [--timeout S]

On success prints the recovered 32-char hex key to stdout and exits 0.
On failure prints a diagnostic to stderr and exits non-zero.

The PUK + VREG flow follows the Victron VeSmart BLE protocol
specification for paired GATT register access.
"""
from __future__ import annotations

import argparse
import binascii
import json
import os
import struct
import sys
import time

import dbus
import dbus.mainloop.glib
import dbus.service

dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
from gi.repository import GLib  # noqa: E402

SVC_306B = "306b0001-b081-4037-83dc-e59fcc3cdfd0"
CTRL_UUID = "306b0002-b081-4037-83dc-e59fcc3cdfd0"
DATA_LAST_UUID = "306b0003-b081-4037-83dc-e59fcc3cdfd0"
DATA_BULK_UUID = "306b0004-b081-4037-83dc-e59fcc3cdfd0"
C6_UUID = "97580006-ddf1-48be-b73e-182664615d8e"
C3_UUID = "97580003-ddf1-48be-b73e-182664615d8e"

AGENT_INTERFACE = "org.bluez.Agent1"


class _PairingAgent(dbus.service.Object):
    """BlueZ pairing agent that returns the Victron device passkey."""

    def __init__(self, bus, path, passkey):
        super().__init__(bus, path)
        self._passkey = passkey

    @dbus.service.method(AGENT_INTERFACE, in_signature="", out_signature="")
    def Release(self):
        pass

    @dbus.service.method(AGENT_INTERFACE, in_signature="os", out_signature="")
    def AuthorizeService(self, device, uuid):
        pass

    @dbus.service.method(AGENT_INTERFACE, in_signature="o", out_signature="u")
    def RequestPasskey(self, device):
        _err(f"Agent: providing passkey {self._passkey:06d}")
        return dbus.UInt32(self._passkey)

    @dbus.service.method(AGENT_INTERFACE, in_signature="ou", out_signature="")
    def RequestConfirmation(self, device, passkey):
        pass

    @dbus.service.method(AGENT_INTERFACE, in_signature="o", out_signature="")
    def RequestAuthorization(self, device):
        pass

    @dbus.service.method(AGENT_INTERFACE, in_signature="", out_signature="")
    def Cancel(self):
        pass


def _err(*a):
    print(*a, file=sys.stderr, flush=True)


def _cbor_uint(n):
    if n < 24:
        return bytes([n])
    if n < 256:
        return bytes([0x18, n])
    if n < 65536:
        return bytes([0x19, (n >> 8) & 0xFF, n & 0xFF])
    return bytes([0x1A, (n >> 24) & 0xFF, (n >> 16) & 0xFF,
                  (n >> 8) & 0xFF, n & 0xFF])


def _cbor_array(items):
    return bytes([0x9F]) + b"".join(items) + bytes([0xFF])


def _scan_for_key(blobs):
    target = bytes([0x19, 0xEC, 0x65, 0x50])
    joined = b"".join(blobs)
    idx = joined.find(target)
    if idx >= 0 and idx + 4 + 16 <= len(joined):
        return joined[idx + 4 : idx + 4 + 16]
    return None


def _scan_for_vreg(blobs, vreg: int):
    """Extract the byte string that a Push response carries for *vreg*.

    Looks for the CBOR encoding of a uint16 register id followed by a
    short (<24 byte) bstr header and returns the payload bytes — or
    ``None`` if no matching entry is found in *blobs*.
    """
    marker = bytes([0x19, (vreg >> 8) & 0xFF, vreg & 0xFF])
    joined = b"".join(blobs)
    idx = 0
    while True:
        idx = joined.find(marker, idx)
        if idx < 0:
            return None
        after = idx + len(marker)
        if after >= len(joined):
            return None
        hdr = joined[after]
        # 0x40-0x57 -> CBOR short bstr, length = hdr & 0x1F
        if 0x40 <= hdr <= 0x57:
            blen = hdr & 0x1F
            start = after + 1
            if start + blen <= len(joined):
                return joined[start : start + blen]
        idx = after


def _find_bluez_device(bus, mac):
    """Find the BlueZ device object path + adapter path across all adapters."""
    om = dbus.Interface(bus.get_object("org.bluez", "/"),
                        "org.freedesktop.DBus.ObjectManager")
    objects = om.GetManagedObjects()
    suffix = "/dev_" + mac.upper().replace(":", "_")
    for path in sorted(objects.keys()):
        if path.endswith(suffix) and "org.bluez.Device1" in objects[path]:
            # adapter path is everything before /dev_
            adapter_path = path[:path.index(suffix)]
            return str(path), str(adapter_path)
    # Fallback: construct from hci0
    return ("/org/bluez/hci0" + suffix,
            "/org/bluez/hci0")


def _ensure_device_known(bus, mac, dev_path, adapter_path, pump_fn,
                         scan_s=8.0):
    """Make sure BlueZ has a live ``org.bluez.Device1`` on *dev_path*.
    After an unbond/remove, BlueZ can drop the Device1 interface until
    it sees another advertisement.  Start discovery briefly so the
    peripheral's advert re-creates the object.
    """
    om = dbus.Interface(
        bus.get_object("org.bluez", "/"),
        "org.freedesktop.DBus.ObjectManager")
    if "org.bluez.Device1" in om.GetManagedObjects().get(dev_path, {}):
        return
    _err(f"Device1 missing on {dev_path} — running {scan_s:.0f}s scan "
         f"on {adapter_path}")
    adapter = dbus.Interface(
        bus.get_object("org.bluez", adapter_path),
        "org.bluez.Adapter1")
    try:
        adapter.StartDiscovery()
    except dbus.DBusException as e:
        _err(f"StartDiscovery: {e}")
    deadline = time.monotonic() + scan_s
    found = False
    while time.monotonic() < deadline:
        pump_fn(500)
        if "org.bluez.Device1" in \
                om.GetManagedObjects().get(dev_path, {}):
            found = True
            break
    try:
        adapter.StopDiscovery()
    except dbus.DBusException:
        pass
    if not found:
        raise RuntimeError(
            f"{mac}: BlueZ never re-created Device1 on {dev_path} "
            f"after {scan_s:.0f}s scan")
    _err(f"Device1 restored on {dev_path}")


def _ensure_paired(bus, dev_path, passkey, pump_fn, timeout_s=45.0):
    """If device isn't already paired, register a KeyboardDisplay agent
    and call Device1.Pair() with *passkey*.  Some Orion-TR firmwares
    (observed on 0xA3D5 48V Buck-Boost) reject unauthenticated writes to
    the PUK characteristic, which would otherwise trigger an SMP pairing
    attempt from BlueZ with the wrong IO capability (DisplayOnly) and
    fail with "Invalid parameters".  Registering the agent up-front fixes
    this for newer firmwares and is a no-op for older ones that don't
    require pairing.

    Returns an (agent, agent_path, agent_mgr) tuple to unregister later,
    or None if the device was already paired.
    """
    dev_obj = bus.get_object("org.bluez", dev_path)
    props = dbus.Interface(dev_obj, "org.freedesktop.DBus.Properties")
    try:
        if bool(props.Get("org.bluez.Device1", "Paired")):
            _err(f"Already Paired=true — skipping pair step")
            return None
    except dbus.DBusException:
        pass

    agent_path = f"/org/victronenergy/orion_tr_keycli/agent_{os.getpid()}"
    agent = _PairingAgent(bus, agent_path, passkey)
    agent_mgr = dbus.Interface(
        bus.get_object("org.bluez", "/org/bluez"),
        "org.bluez.AgentManager1")
    try:
        agent_mgr.UnregisterAgent(dbus.ObjectPath(agent_path))
    except dbus.DBusException:
        pass
    agent_mgr.RegisterAgent(dbus.ObjectPath(agent_path), "KeyboardDisplay")
    agent_mgr.RequestDefaultAgent(dbus.ObjectPath(agent_path))
    _err(f"Agent registered at {agent_path} (passkey {passkey:06d})")

    try:
        props.Set("org.bluez.Device1", "Trusted", dbus.Boolean(True))
    except dbus.DBusException as e:
        _err(f"Trusted set failed (non-fatal): {e}")

    device = dbus.Interface(dev_obj, "org.bluez.Device1")
    result = {"done": False, "err": None}

    def on_ok():
        result["done"] = True
        _err("Pair() reply OK")

    def on_err(e):
        result["done"] = True
        result["err"] = e
        _err(f"Pair() error: {e}")

    device.Pair(reply_handler=on_ok, error_handler=on_err,
                timeout=timeout_s)

    deadline = time.monotonic() + timeout_s
    while not result["done"] and time.monotonic() < deadline:
        pump_fn(200)

    try:
        paired = bool(props.Get("org.bluez.Device1", "Paired"))
    except dbus.DBusException:
        paired = False

    if not paired:
        try:
            agent_mgr.UnregisterAgent(dbus.ObjectPath(agent_path))
        except Exception:
            pass
        raise RuntimeError(
            f"Pair() did not complete (done={result['done']}, "
            f"err={result['err']}, Paired={paired})")

    _err(f"Paired with {dev_path}")
    return (agent, agent_path, agent_mgr)


def _cleanup_agent(agent_info):
    if not agent_info:
        return
    _agent, agent_path, agent_mgr = agent_info
    try:
        agent_mgr.UnregisterAgent(dbus.ObjectPath(agent_path))
    except Exception:
        pass


def provision(mac, passkey, timeout_s, preferred_adapter=None):
    bus = dbus.SystemBus()
    dev_path, adapter_path = _find_bluez_device(bus, mac)

    # If a preferred adapter is known from a prior successful connection,
    # rewrite the paths to try it first.
    if preferred_adapter:
        pref_path = f"/org/bluez/{preferred_adapter}"
        pref_dev = f"{pref_path}/dev_{mac.upper().replace(':', '_')}"
        try:
            bus.get_object("org.bluez", pref_dev, introspect=False)
            _err(f"Using preferred adapter {preferred_adapter}")
            dev_path = pref_dev
            adapter_path = pref_path
        except Exception:
            _err(f"Preferred adapter {preferred_adapter} doesn't know "
                 f"this device — falling back to {adapter_path}")

    _err(f"Using {adapter_path} for {mac} (device {dev_path})")
    ctx = GLib.MainContext.default()

    def pump(ms):
        end = time.monotonic() + ms / 1000.0
        while time.monotonic() < end:
            ctx.iteration(False)
            time.sleep(0.005)

    # Ensure SMP pairing / bonding exists before any GATT writes.  Some
    # Orion-TR firmwares require an authenticated link to write to the
    # PUK char (97580006); without a BlueZ agent registered, BlueZ would
    # auto-pair with the wrong IO capability and the peripheral would
    # reject with "Invalid parameters".  Register cleanup via atexit so
    # the agent is always unregistered, regardless of how provision()
    # exits (success, raised exception, sys.exit, kill).
    import atexit
    _ensure_device_known(bus, mac, dev_path, adapter_path, pump)
    agent_info = _ensure_paired(bus, dev_path, passkey, pump)
    if agent_info is not None:
        atexit.register(_cleanup_agent, agent_info)

    collected = []
    bulk_buf = bytearray()

    def on_last(_i, changed, _inv):
        if "Value" not in changed:
            return
        data = bytes(int(b) for b in changed["Value"])
        full = bytes(bulk_buf) + data
        bulk_buf.clear()
        collected.append(full)
        _err(f"[LAST] {len(full)}B: {full.hex()}")

    def on_bulk(_i, changed, _inv):
        if "Value" not in changed:
            return
        data = bytes(int(b) for b in changed["Value"])
        bulk_buf.extend(data)
        _err(f"[BULK] +{len(data)}B: {data.hex()}")

    # --- Connect ---
    # On multi-adapter Cerbos a device may be visible on one adapter but
    # only connectable from another.  Try the primary; on failure, scan
    # all adapters for the same MAC and retry.
    # le-connection-abort-by-local is a transient BlueZ error (scan/conn
    # race) — retry the primary adapter a few times before falling back
    # to alternates, since fallback-to-low-RSSI can "connect" then hang.
    # Disconnect any stale session on ALL adapters first — after a
    # prior successful provisioning the device-side session lingers
    # and a re-PUK may be rejected until the link is torn down and
    # re-established.
    om_pre = dbus.Interface(bus.get_object("org.bluez", "/"),
                            "org.freedesktop.DBus.ObjectManager")
    suffix_pre = "/dev_" + mac.upper().replace(":", "_")
    for p in sorted(om_pre.GetManagedObjects().keys()):
        p = str(p)
        if p.endswith(suffix_pre):
            try:
                dbus.Interface(
                    bus.get_object("org.bluez", p),
                    "org.bluez.Device1").Disconnect()
                _err(f"Pre-disconnected {p}")
            except dbus.DBusException:
                pass
    pump(1500)

    _err(f"Connecting to {mac} via {dev_path}...")
    connected = False
    for attempt in range(1, 5):
        try:
            dev_obj = bus.get_object("org.bluez", dev_path)
            dbus.Interface(dev_obj, "org.bluez.Device1").Connect()
            connected = True
            break
        except dbus.DBusException as e:
            if "Already Connected" in str(e):
                dev_obj = bus.get_object("org.bluez", dev_path)
                connected = True
                break
            _err(f"  Connect attempt {attempt}/4 on {dev_path}: {e}")
            pump(1500)

    if not connected:
        # Try every other adapter that knows this MAC
        om = dbus.Interface(bus.get_object("org.bluez", "/"),
                            "org.freedesktop.DBus.ObjectManager")
        suffix = "/dev_" + mac.upper().replace(":", "_")
        for p in sorted(om.GetManagedObjects().keys()):
            p = str(p)
            if p.endswith(suffix) and p != dev_path:
                _err(f"  Trying alternate {p}...")
                try:
                    dev_obj = bus.get_object("org.bluez", p)
                    dbus.Interface(dev_obj, "org.bluez.Device1").Connect()
                    dev_path = p
                    adapter_path = p[:p.index(suffix)]
                    connected = True
                    _err(f"  Connected via {adapter_path}")
                    break
                except dbus.DBusException as e2:
                    if "Already Connected" in str(e2):
                        dev_obj = bus.get_object("org.bluez", p)
                        dev_path = p
                        connected = True
                        break
                    _err(f"  Also failed: {e2}")

    if not connected:
        raise RuntimeError(f"Cannot connect to {mac} on any adapter")

    device = dbus.Interface(dev_obj, "org.bluez.Device1")
    dev_props = dbus.Interface(dev_obj, "org.freedesktop.DBus.Properties")
    services_ok = False
    for _ in range(60):  # up to 30s — post-pair discovery can be slow
        pump(500)
        try:
            if bool(dev_props.Get("org.bluez.Device1", "ServicesResolved")):
                services_ok = True
                break
        except dbus.DBusException:
            pass

    # If services didn't resolve, the adapter may have a stale
    # connection.  Disconnect and try other adapters.
    if not services_ok:
        _err(f"ServicesResolved timed out on {dev_path} — trying other adapters")
        try:
            device.Disconnect()
        except Exception:
            pass
        pump(1000)
        om = dbus.Interface(bus.get_object("org.bluez", "/"),
                            "org.freedesktop.DBus.ObjectManager")
        suffix = "/dev_" + mac.upper().replace(":", "_")
        for p in sorted(om.GetManagedObjects().keys()):
            p = str(p)
            if p.endswith(suffix) and p != dev_path:
                _err(f"  Trying {p}...")
                try:
                    dev_obj = bus.get_object("org.bluez", p)
                    dbus.Interface(dev_obj, "org.bluez.Device1").Connect()
                    dev_props = dbus.Interface(dev_obj,
                                              "org.freedesktop.DBus.Properties")
                    for _ in range(60):  # up to 30s
                        pump(500)
                        try:
                            if bool(dev_props.Get("org.bluez.Device1",
                                                  "ServicesResolved")):
                                services_ok = True
                                dev_path = p
                                device = dbus.Interface(dev_obj,
                                                        "org.bluez.Device1")
                                _err(f"  Services resolved on {p}")
                                break
                        except dbus.DBusException:
                            pass
                    if services_ok:
                        break
                except dbus.DBusException as e2:
                    _err(f"  Failed: {e2}")

    if not services_ok:
        raise RuntimeError("ServicesResolved never fired on any adapter")
    _err("Connected.")

    # --- Discover chars ---
    om = dbus.Interface(bus.get_object("org.bluez", "/"),
                        "org.freedesktop.DBus.ObjectManager")
    objects = om.GetManagedObjects()
    chars_9758 = {}
    chars_306b = {}
    for path, ifs in objects.items():
        if "org.bluez.GattCharacteristic1" not in ifs:
            continue
        if not path.startswith(dev_path):
            continue
        cp = ifs["org.bluez.GattCharacteristic1"]
        uuid = str(cp.get("UUID", ""))
        svc_path = str(cp.get("Service", ""))
        if uuid.startswith("9758"):
            chars_9758[uuid] = path
        elif uuid.startswith("306b") and svc_path in objects:
            si = objects[svc_path]
            if "org.bluez.GattService1" in si:
                if str(si["org.bluez.GattService1"].get("UUID", "")) == SVC_306B:
                    chars_306b[uuid] = path

    if C6_UUID not in chars_9758:
        raise RuntimeError("PUK characteristic 9758…06 not found")
    if CTRL_UUID not in chars_306b or DATA_LAST_UUID not in chars_306b:
        raise RuntimeError("306b CTRL/DATA_LAST not found")

    ci6 = dbus.Interface(bus.get_object("org.bluez", chars_9758[C6_UUID]),
                         "org.bluez.GattCharacteristic1")
    ctrl = dbus.Interface(bus.get_object("org.bluez", chars_306b[CTRL_UUID]),
                          "org.bluez.GattCharacteristic1")
    dlast = dbus.Interface(bus.get_object("org.bluez", chars_306b[DATA_LAST_UUID]),
                           "org.bluez.GattCharacteristic1")

    # On Venus OS, StartNotify + PropertiesChanged signals deliver empty
    # payloads for the 306b characteristics when the link is SMP-paired
    # (see 9758-VESERVICE-RESEARCH.md).  Use AcquireNotify instead: it
    # returns a Unix fd that delivers raw ATT notification bytes, which
    # we read in a GLib IO watch.
    notify_fds = []

    def _acquire_notify(char_iface, char_path, label, on_bytes):
        try:
            ret = char_iface.AcquireNotify({})
            # ret is (fd: dbus.types.UnixFd, mtu: uint16)
            fd = ret[0].take() if hasattr(ret[0], "take") else int(ret[0])
            mtu = int(ret[1]) if len(ret) > 1 else 0
            _err(f"AcquireNotify {label}: fd={fd} mtu={mtu}")
            notify_fds.append(fd)

            def on_io(source, cond):
                if cond & (GLib.IO_HUP | GLib.IO_ERR | GLib.IO_NVAL):
                    _err(f"{label} fd closed (cond={cond})")
                    return False
                try:
                    data = os.read(source, 512)
                except OSError as e:
                    _err(f"{label} read: {e}")
                    return False
                if data:
                    on_bytes(data)
                return True

            GLib.io_add_watch(
                fd, GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, on_io)
            return True
        except dbus.DBusException as e:
            _err(f"AcquireNotify {label} failed ({e}); falling back "
                 f"to StartNotify")
            return False

    def on_last_bytes(data):
        full = bytes(bulk_buf) + data
        bulk_buf.clear()
        collected.append(full)
        _err(f"[LAST] {len(full)}B: {full.hex()}")

    def on_bulk_bytes(data):
        bulk_buf.extend(data)
        _err(f"[BULK] +{len(data)}B: {data.hex()}")

    def on_ctrl_bytes(data):
        # Just log device-side CTRL traffic (F7 Error, F9 credits, F8
        # buffer-clear).  The handshake is driven explicitly below by
        # a CTRL ReadValue + FA/F9 writes — do NOT do it here or we
        # race ourselves.
        if data:
            _err(f"[CTRL-RX] {len(data)}B: {data.hex()}")

    # Enable notifications on CTRL first — the device needs CCCD on
    # 306b0002 before it will push its session header to us.
    if not _acquire_notify(ctrl, chars_306b[CTRL_UUID],
                           "CTRL", on_ctrl_bytes):
        bus.add_signal_receiver(
            lambda _i, ch, _inv: on_ctrl_bytes(
                bytes(int(b) for b in ch["Value"])
                if "Value" in ch else b""),
            dbus_interface="org.freedesktop.DBus.Properties",
            signal_name="PropertiesChanged",
            path=chars_306b[CTRL_UUID])
        ctrl.StartNotify()

    if not _acquire_notify(dlast, chars_306b[DATA_LAST_UUID],
                           "DATA_LAST", on_last_bytes):
        bus.add_signal_receiver(
            on_last,
            dbus_interface="org.freedesktop.DBus.Properties",
            signal_name="PropertiesChanged",
            path=chars_306b[DATA_LAST_UUID])
        dlast.StartNotify()

    if DATA_BULK_UUID in chars_306b:
        dbulk = dbus.Interface(
            bus.get_object("org.bluez", chars_306b[DATA_BULK_UUID]),
            "org.bluez.GattCharacteristic1")
        if not _acquire_notify(dbulk, chars_306b[DATA_BULK_UUID],
                               "DATA_BULK", on_bulk_bytes):
            bus.add_signal_receiver(
                on_bulk,
                dbus_interface="org.freedesktop.DBus.Properties",
                signal_name="PropertiesChanged",
                path=chars_306b[DATA_BULK_UUID])
            dbulk.StartNotify()

    pump(500)

    # --- Always-on PUK notify receiver (used by optional auth below) ---
    puk_responses = []

    def on_puk(_i, changed, _inv):
        if "Value" not in changed:
            return
        puk_responses.append(bytes(int(b) for b in changed["Value"]))
        _err(f"[PUK] {len(puk_responses[-1])}B: {puk_responses[-1].hex()}")

    bus.add_signal_receiver(
        on_puk,
        dbus_interface="org.freedesktop.DBus.Properties",
        signal_name="PropertiesChanged",
        path=chars_9758[C6_UUID])
    ci6.StartNotify()
    pump(200)

    def _do_puk_pin_auth():
        """Full PUK CRC + PIN auth on 9758 service.  Needed on the very
        first provisioning of a device whose firmware enforces the
        PUK+PIN path; already-bonded devices skip this (fast path)."""
        # PUK CRC
        puk_ok = False
        for attempt in range(1, 4):
            puk_responses.clear()
            nonce = bytes(int(b) for b in ci6.ReadValue({}))
            crc = binascii.crc32(nonce) & 0xFFFFFFFF
            crc_bytes = struct.pack("<I", crc)
            _err(f"PUK auth attempt {attempt}: nonce={nonce.hex()} "
                 f"crc={crc_bytes.hex()}")
            ci6.WriteValue(list(crc_bytes), {"type": "command"})
            pump(1500)
            if any(d == b"\x00" for d in puk_responses):
                puk_ok = True
                _err("PUK CRC OK")
                break
            _err(f"PUK attempt {attempt} rejected (responses="
                 f"{[d.hex() for d in puk_responses]})")
            pump(500)
        if not puk_ok:
            raise RuntimeError("PUK CRC not accepted after 3 attempts")

        # PIN auth on c3 (97580003) — nonce + LE32(passkey), 12 bytes.
        # Needed on 0xA3D5 firmware; harmless on older devices.
        if C3_UUID in chars_9758:
            c3 = dbus.Interface(
                bus.get_object("org.bluez", chars_9758[C3_UUID]),
                "org.bluez.GattCharacteristic1")
            c3_responses = []

            def on_c3(_i, changed, _inv):
                if "Value" not in changed:
                    return
                c3_responses.append(
                    bytes(int(b) for b in changed["Value"]))
                _err(f"[C3] {len(c3_responses[-1])}B: "
                     f"{c3_responses[-1].hex()}")

            bus.add_signal_receiver(
                on_c3,
                dbus_interface="org.freedesktop.DBus.Properties",
                signal_name="PropertiesChanged",
                path=chars_9758[C3_UUID])
            try:
                c3.StartNotify()
                pump(200)
                fresh_nonce = bytes(
                    int(b) for b in ci6.ReadValue({}))
                pin_payload = (fresh_nonce
                               + struct.pack("<I", passkey))
                _err(f"PIN auth: nonce+PIN = {pin_payload.hex()}")
                c3.WriteValue(list(pin_payload), {"type": "command"})
                pump(2000)
                if any(r == b"\x00" for r in c3_responses):
                    _err("PIN accepted")
                else:
                    _err(f"PIN responses="
                         f"{[r.hex() for r in c3_responses]}"
                         f" — continuing anyway")
            except dbus.DBusException as e:
                _err(f"PIN step failed (non-fatal): {e}")

    # --- CTRL READ (critical!) ---
    # Reading the CTRL characteristic triggers the device's CBOR-mode
    # initialisation.  Skipping the read leaves the device's response
    # pipeline disabled — DATA_LAST never fires.  Do the read after
    # auth and follow up with our FA/F9 handshake writes.
    try:
        ctrl_hdr = bytes(int(b) for b in ctrl.ReadValue({}))
        _err(f"CTRL header: {ctrl_hdr.hex()}")
    except dbus.DBusException as e:
        _err(f"CTRL ReadValue: {e} — proceeding anyway")
    ctrl.WriteValue([0xFA, 0x80, 0xFF], {"type": "command"})
    pump(300)
    ctrl.WriteValue([0xF9, 0x80], {"type": "command"})
    pump(400)

    # --- Prime: Subscribe to a chatty public VREG to wake the pipe ---
    # A GetValue 0x25 sent as the very first CBOR request sometimes
    # gets swallowed before the device has established its outgoing
    # stream.  Sending a plain subscribe first forces the device to
    # start pushing and keeps credits flowing.
    prime = bytes([0x03, 0x00, 0x9F, 0x19, 0xED, 0xDB, 0xFF])
    _err(f"Subscribe 0xEDDB (prime): {prime.hex()}")
    dlast.WriteValue(list(prime), {"type": "command"})
    prime_deadline = time.monotonic() + 3.0
    while time.monotonic() < prime_deadline and not collected:
        pump(400)
        try:
            ctrl.WriteValue([0xF9, 0x80], {"type": "command"})
        except Exception:
            pass

    # --- GetValue 0xEC65 (fast path, then PUK+PIN fallback) ---
    # Opcode 0x25 (not 0x05) is the "privileged" GetValue variant for
    # the advertisement-key register.  Plain 0x05 returns
    # "RequestedEncryptionNotSupported" (error code 2) for this reg.
    # The fast path skips PUK+PIN — sufficient for already-bonded
    # devices (e.g. daily refresh).  On error 2 we fall back to full
    # auth and retry once.
    cmd = bytes([0x25, 0x00, 0x9F, 0x19, 0xEC, 0x65, 0xFF])

    def _scan_for_encryption_error(blobs):
        # ACK error 2 format: `07 19 EC 65 05 02` or `07 19 EC 65 25 02`.
        # Any frame ending in `19 EC 65 <opcode> 02` signals encryption
        # refused.
        j = b"".join(blobs)
        for tail in (b"\x19\xec\x65\x05\x02",
                     b"\x19\xec\x65\x25\x02"):
            if tail in j:
                return True
        return False

    key = None
    for attempt_phase in ("fast", "authed"):
        collected.clear()
        bulk_buf.clear()
        _err(f"GetValue 0xEC65 ({attempt_phase}, opcode 0x25): "
             f"{cmd.hex()}")
        dlast.WriteValue(list(cmd), {"type": "command"})
        phase_deadline = time.monotonic() + (
            min(timeout_s, 15.0) if attempt_phase == "authed" else 6.0)
        refused = False
        while time.monotonic() < phase_deadline:
            pump(500)
            key = _scan_for_key(collected)
            if key is not None:
                _err(f"Recovered key: {len(key)}B")
                break
            if _scan_for_encryption_error(collected):
                refused = True
                _err("Device refused with encryption error — "
                     "falling back to PUK+PIN auth")
                break
            try:
                ctrl.WriteValue([0xF9, 0x80], {"type": "command"})
            except Exception:
                pass
        if key is not None:
            break
        if attempt_phase == "fast" and refused:
            _do_puk_pin_auth()
            # Re-do CTRL read + handshake after auth (session may reset)
            try:
                _ = bytes(int(b) for b in ctrl.ReadValue({}))
            except dbus.DBusException:
                pass
            try:
                ctrl.WriteValue([0xFA, 0x80, 0xFF], {"type": "command"})
                pump(300)
                ctrl.WriteValue([0xF9, 0x80], {"type": "command"})
                pump(400)
            except dbus.DBusException:
                pass
            # Re-prime
            dlast.WriteValue(
                list(bytes([0x03, 0x00, 0x9F, 0x19, 0xED, 0xDB, 0xFF])),
                {"type": "command"})
            pump(1500)
            continue
        # fast succeeded → we already broke above; or authed failed → exit
        break

    if key is None:
        try:
            device.Disconnect()
        except Exception:
            pass
        raise RuntimeError(
            f"no 16-byte key in VREG 0xEC65 response "
            f"({len(collected)} chunks, "
            f"{sum(len(c) for c in collected)}B total)")

    # --- Opportunistically read firmware (0x0140) and product id ------
    # (0x0100) in the same paired session, one more GetValue round-trip
    # each.  We don't fail the whole flow if a register is unavailable —
    # some firmwares may not expose a particular register.
    def _fetch_vreg(vreg: int, label: str,
                    timeout: float = 4.0) -> str:
        try:
            req = (_cbor_uint(0x05) + _cbor_uint(0)
                   + _cbor_array([_cbor_uint(vreg)]))
            collected.clear()
            bulk_buf.clear()
            _err(f"GetValue 0x{vreg:04X} ({label}): {req.hex()}")
            dlast.WriteValue(list(req), {"type": "request"})
            deadline_local = time.monotonic() + timeout
            while time.monotonic() < deadline_local:
                pump(400)
                val = _scan_for_vreg(collected, vreg)
                if val is not None:
                    _err(f"Recovered {label} bytes: {val.hex()}")
                    return val.hex()
                try:
                    ctrl.WriteValue([0xF9, 0x08], {"type": "command"})
                except Exception:
                    pass
        except Exception as e:
            _err(f"{label} read failed (non-fatal): {e}")
        return None

    firmware_hex = _fetch_vreg(0x0140, "firmware")
    product_id_hex = _fetch_vreg(0x0100, "product id")
    temperature_hex = _fetch_vreg(0xEDDB, "temperature")

    # Read DeviceInfo (97580002) for hardware version — this is a plain
    # GATT ReadValue, no CBOR/flow-control needed.
    hw_version = None
    try:
        if "97580002-ddf1-48be-b73e-182664615d8e" in chars_9758:
            di_iface = dbus.Interface(
                bus.get_object("org.bluez",
                               chars_9758["97580002-ddf1-48be-b73e-182664615d8e"]),
                "org.bluez.GattCharacteristic1")
            di_val = bytes(int(b) for b in di_iface.ReadValue({}))
            _err(f"DeviceInfo: {len(di_val)}B: {di_val.hex()}")
            if len(di_val) >= 4:
                hw_rev = int.from_bytes(di_val[2:4], "little")
                hw_version = str(hw_rev)
                _err(f"Hardware revision: {hw_version}")
    except Exception as e:
        _err(f"DeviceInfo read failed (non-fatal): {e}")

    for _fd in notify_fds:
        try:
            os.close(_fd)
        except Exception:
            pass
    notify_fds.clear()
    try:
        device.Disconnect()
    except Exception:
        pass

    # Extract which adapter we ended up using (e.g. "hci1") so the
    # caller can persist it for next time.
    used_adapter = adapter_path.rsplit("/", 1)[-1] if "/" in adapter_path \
        else adapter_path

    return {
        "key": key.hex(),
        "firmware": firmware_hex,
        "product_id": product_id_hex,
        "temperature": temperature_hex,
        "hardware_version": hw_version,
        "adapter": used_adapter,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("mac")
    ap.add_argument("--passkey", type=int, default=0)
    ap.add_argument("--timeout", type=float, default=40.0)
    ap.add_argument("--preferred-adapter", default=None,
                    help="Try this adapter first (e.g. hci1)")
    args = ap.parse_args()
    try:
        result = provision(args.mac, args.passkey, args.timeout,
                           preferred_adapter=args.preferred_adapter)
    except Exception as e:
        _err(f"orion-tr key provisioning failed: {e}")
        return 1
    sys.stdout.write(json.dumps(result) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
