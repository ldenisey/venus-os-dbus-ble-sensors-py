#!/usr/bin/env python3
import sys
import os
sys.path.insert(1, os.path.join(os.path.dirname(__file__), 'ext'))
sys.path.insert(1, os.path.join(os.path.dirname(__file__), 'ext', 'velib_python'))
import logging
from logging.handlers import RotatingFileHandler
import dbus
from dbus.mainloop.glib import DBusGMainLoop
from argparse import ArgumentParser
from ble_device import BleDevice
from ble_device_orion_tr import BleDeviceOrionTR, is_orion_tr_manufacturer_data
from ble_device_ip22_charger import (
    BleDeviceIP22Charger,
    is_ip22_charger_manufacturer_data,
)
from ble_role import BleRole
from dbus_bus import get_bus
from dbus_ble_service import DbusBleService
from gi.repository import GLib
from logger import setup_logging
from collections.abc import MutableMapping
import threading
import time
from conf import IGNORED_DEVICES_TIMEOUT, DEVICE_SERVICES_TIMEOUT, PROCESS_VERSION
from hci_advertisement_tap import (
    create_tap_socket, run_tap_loop, TappedAdvertisement,
)
from ble_advertisement_router import BleAdvertisementRouter

ADV_LOG_QUIET_PERIOD = 1800
SILENCE_WARNING_SECONDS = 300
DEDUP_KEEPALIVE_SECONDS = 900  # re-forward identical data every 15 min
from man_id import MAN_NAMES

SNIF_LOGGER = logging.getLogger("sniffer")
SNIF_LOGGER.propagate = False

_MONITOR_IFACE = 'org.bluez.AdvertisementMonitor1'
_PROPS_IFACE = 'org.freedesktop.DBus.Properties'
_OM_IFACE = 'org.freedesktop.DBus.ObjectManager'
_MONITOR_APP_PATH = '/org/bluez/ble_sensors'
_MONITOR_OBJ_PATH = _MONITOR_APP_PATH + '/0'

_CATCH_ALL_PATTERN = dbus.Struct(
    [dbus.Byte(0), dbus.Byte(0x01), dbus.Array([dbus.Byte(0x06)], signature='y')],
    signature=None,
)

_MONITOR_PROPS = {
    'Type': dbus.String('or_patterns'),
    'Patterns': dbus.Array([_CATCH_ALL_PATTERN], signature='(yyay)'),
}

class _MonitorApp(dbus.service.Object):
    """ObjectManager root that exposes AdvertisementMonitor children to BlueZ.

    BlueZ's RegisterMonitor API uses g_dbus_client which calls
    GetManagedObjects on the registered root to discover child objects
    implementing AdvertisementMonitor1.
    """

    def __init__(self, bus: dbus.bus.BusConnection, path: str, child_path: str):
        super().__init__(bus, path)
        self._child_path = child_path

    @dbus.service.method(_OM_IFACE, in_signature='', out_signature='a{oa{sa{sv}}}')
    def GetManagedObjects(self):
        return dbus.Dictionary({
            dbus.ObjectPath(self._child_path): dbus.Dictionary({
                _MONITOR_IFACE: dbus.Dictionary(_MONITOR_PROPS, signature='sv'),
            }, signature='sa{sv}'),
        }, signature='oa{sa{sv}}')

class _AdvMonitor(dbus.service.Object):
    """AdvertisementMonitor1 implementation for passive BLE scanning.

    Registers a broad or_patterns monitor with BlueZ so the controller
    performs passive scanning.  We match the common LE Flags byte (AD type
    0x01, value 0x06 = General Discoverable | BR/EDR Not Supported) which
    captures virtually all BLE peripherals.  The HCI tap does its own
    manufacturer-ID filtering so this pattern is intentionally wide.
    """

    def __init__(self, bus: dbus.bus.BusConnection, path: str,
                 on_release=None):
        super().__init__(bus, path)
        self._on_release = on_release

    @dbus.service.method(_MONITOR_IFACE, in_signature='', out_signature='')
    def Release(self):
        logging.warning("AdvMonitor: released by BlueZ — will re-register")
        if self._on_release:
            self._on_release()

    @dbus.service.method(_MONITOR_IFACE, in_signature='', out_signature='')
    def Activate(self):
        logging.info("AdvMonitor: passive scanning activated by BlueZ")

    @dbus.service.method(_MONITOR_IFACE, in_signature='o', out_signature='')
    def DeviceFound(self, device):
        pass

    @dbus.service.method(_MONITOR_IFACE, in_signature='o', out_signature='')
    def DeviceLost(self, device):
        pass

    @dbus.service.method(_PROPS_IFACE, in_signature='ss', out_signature='v')
    def Get(self, interface, prop):
        if interface == _MONITOR_IFACE:
            if prop == 'Type':
                return _MONITOR_PROPS['Type']
            if prop == 'Patterns':
                return _MONITOR_PROPS['Patterns']
        raise dbus.exceptions.DBusException(
            f'No property {prop}',
            name='org.freedesktop.DBus.Error.InvalidArgs')

    @dbus.service.method(_PROPS_IFACE, in_signature='s', out_signature='a{sv}')
    def GetAll(self, interface):
        if interface == _MONITOR_IFACE:
            return dbus.Dictionary(_MONITOR_PROPS, signature='sv')
        return dbus.Dictionary({}, signature='sv')

class DbusBleSensors(object):
    """
    Main class for the D-bus BLE Sensors python service.
    Extends base C service 'dbus-ble-sensors' to allow community integration of any BLE sensors.

    BLE advertisements are received via an HCI monitor channel tap — a passive
    read-only socket that sees ALL HCI traffic between the host and every
    Bluetooth controller (the same mechanism btmon uses).

    To make the controller actually scan, we register an AdvertisementMonitor1
    with BlueZ on each adapter.  This triggers *passive* scanning — the
    controller listens for advertisements without sending SCAN_REQ packets —
    which coexists cleanly with other services that need active scanning and
    GATT connections (e.g. power-watchdog, shyion-switch via bleak).

    Cf.
    - https://github.com/victronenergy/dbus-ble-sensors/
    - https://github.com/victronenergy/node-red-contrib-victron/blob/master/src/nodes/victron-virtual.js
    - https://github.com/victronenergy/gui-v2/blob/main/data/mock/conf/services/ruuvi-salon.json
    """

    def __init__(self):
        self._dbus: dbus.bus.BusConnection = get_bus("org.bluez")
        self._dbus_ble_service = DbusBleService()

        self._adapters = []
        self._adapter_paths: dict[str, str] = {}

        self._known_mac = DatedDict(ttl=DEVICE_SERVICES_TIMEOUT)
        self._ignored_mac = DatedDict(ttl=IGNORED_DEVICES_TIMEOUT)
        self._last_adv_seen: dict[str, float] = {}

        BleRole.load_classes(os.path.abspath(__file__))
        BleDevice.load_classes(os.path.abspath(__file__))

        self._internal_mfg_ids: frozenset[int] = frozenset(BleDevice.DEVICE_CLASSES.keys())
        self._known_mfg_ids: set[int] = set(self._internal_mfg_ids)
        self._last_mfg_data: dict[str, tuple[bytes, float]] = {}
        self._tap_seen_macs: dict[str, float] = {}
        self._tap_ignored_macs: set[str] = set()
        self._last_tap_rx: float = 0.0
        self._silence_warned: bool = False
        self._tap_thread: threading.Thread | None = None
        self._tap_stop = threading.Event()
        self._monitor_app = _MonitorApp(self._dbus, _MONITOR_APP_PATH, _MONITOR_OBJ_PATH)
        self._registered_adapters: set[str] = set()
        self._monitor_obj = _AdvMonitor(
            self._dbus, _MONITOR_OBJ_PATH,
            on_release=self._registered_adapters.clear,
        )

        self._router = BleAdvertisementRouter(
            self._dbus,
            version=PROCESS_VERSION,
            on_registrations_changed=self._on_registrations_changed,
        )

        self._list_adapters()

    def _list_adapters(self):
        self._dbus.add_signal_receiver(
            self._on_interfaces_added,
            dbus_interface='org.freedesktop.DBus.ObjectManager',
            signal_name='InterfacesAdded'
        )
        self._dbus.add_signal_receiver(
            self._on_interfaces_removed,
            dbus_interface='org.freedesktop.DBus.ObjectManager',
            signal_name='InterfacesRemoved'
        )

        object_manager = dbus.Interface(
            self._dbus.get_object('org.bluez', '/'),
            'org.freedesktop.DBus.ObjectManager'
        )
        objects = object_manager.GetManagedObjects()
        for path, ifaces in objects.items():
            self._on_interfaces_added(path, ifaces)

    def _on_interfaces_added(self, path, interfaces):
        if not str(path).startswith('/org/bluez'):
            return
        name = path.split('/')[-1]
        if 'org.bluez.Adapter1' in interfaces:
            adapter = self._dbus.get_object('org.bluez', path)
            props = dbus.Interface(adapter, 'org.freedesktop.DBus.Properties')
            mac = props.Get('org.bluez.Adapter1', 'Address')
            logging.info(f"{name}: adding adapter, path={path!r}, address={mac!r}")
            if name not in self._adapters:
                self._adapters.append(name)
                self._adapter_paths[name] = str(path)
                self._dbus_ble_service.add_ble_adapter(name, mac)
                self._register_passive_monitor(name)

    def _on_interfaces_removed(self, path, interfaces):
        if not str(path).startswith('/org/bluez'):
            return
        name = path.split('/')[-1]
        if 'org.bluez.Adapter1' in interfaces:
            self._dbus_ble_service.remove_ble_adapter(name)
            self._adapters.remove(name)
            self._adapter_paths.pop(name, None)
            self._registered_adapters.discard(name)
            logging.info(f"{name}: adapter removed")

    def _register_passive_monitor(self, adapter_name: str):
        """Register the AdvertisementMonitor app with a BlueZ adapter.

        The HCI monitor tap is read-only — it sees traffic but cannot tell
        the controller to scan.  This registers our AdvertisementMonitor1
        hierarchy with BlueZ which triggers passive scanning on the adapter.
        Unlike StartDiscovery (active scanning), this coexists cleanly with
        other services that need active scans and GATT connections.

        Uses async D-Bus call to avoid deadlock: BlueZ needs to call
        GetManagedObjects on our root during registration, which requires
        the GLib main loop to dispatch the incoming call.
        """
        adapter_path = self._adapter_paths.get(adapter_name)
        if not adapter_path or adapter_name in self._registered_adapters:
            return
        try:
            adapter = self._dbus.get_object('org.bluez', adapter_path)
            mgr = dbus.Interface(adapter, 'org.bluez.AdvertisementMonitorManager1')
            mgr.RegisterMonitor(
                _MONITOR_APP_PATH,
                reply_handler=lambda: self._on_monitor_registered(adapter_name),
                error_handler=lambda exc: self._on_monitor_register_failed(adapter_name, exc),
            )
        except dbus.exceptions.DBusException as exc:
            logging.warning(f"{adapter_name}: failed to register monitor: {exc}")

    def _on_monitor_registered(self, adapter_name: str):
        self._registered_adapters.add(adapter_name)
        logging.info(f"{adapter_name}: passive scanning monitor registered")

    def _on_monitor_register_failed(self, adapter_name: str, exc):
        logging.warning(f"{adapter_name}: failed to register monitor: {exc}")

    def _process_advertisement(self, dev_mac: str, manufacturer_data: dict[int, bytes],
                               adapter_index: int = 0, rssi: int = 0):
        """Process a single BLE advertisement (called on the GLib main thread).

        Each (mfg_id, data) pair is offered to both the internal device class
        system and the external advertisement router.  A MAC is only added to
        the ignore list when *neither* system is interested.
        """
        if dev_mac in self._ignored_mac:
            if dev_mac not in self._known_mac:
                return
            del self._ignored_mac[dev_mac]
            logging.debug(f"{dev_mac}: recovered known device from ignored list")

        adapter_name = f"hci{adapter_index}"

        for man_id, man_data in manufacturer_data.items():
            routed = self._router.process_advertisement(
                dev_mac, man_id, man_data, rssi, adapter_name)

            if dev_mac not in self._known_mac:
                self.snif_data(man_id, man_data)

                # Victron manufacturer id 0x02E1: Orion-TR Smart, IP22 charger or SolarSense
                if man_id == 0x02E1 and is_orion_tr_manufacturer_data(man_data):
                    device_class = BleDeviceOrionTR
                elif man_id == 0x02E1 and is_ip22_charger_manufacturer_data(man_data):
                    device_class = BleDeviceIP22Charger
                else:
                    device_class = BleDevice.DEVICE_CLASSES.get(man_id, None)
                if device_class is None:
                    if not routed:
                        now = time.monotonic()
                        if now - self._last_adv_seen.get(dev_mac, 0) >= ADV_LOG_QUIET_PERIOD:
                            logging.info(f"{dev_mac}: ignoring manufacturer {man_id:#06x}, no device class")
                        self._last_adv_seen[dev_mac] = now
                        self._ignored_mac[dev_mac] = True
                        self._tap_ignored_macs.add(dev_mac)
                    continue

                logging.info(f"{dev_mac}: initializing device with class {device_class}")
                try:
                    dev_instance = device_class(dev_mac)
                    if not dev_instance.check_manufacturer_data(man_data):
                        logging.info(
                            f"{dev_mac}: manufacturer data check failed for "
                            f"{device_class.__name__}, ignoring")
                        if not routed:
                            self._ignored_mac[dev_mac] = True
                            self._tap_ignored_macs.add(dev_mac)
                        continue
                    dev_instance.configure(man_data)
                    dev_instance.init()
                    self._known_mac[dev_mac] = dev_instance
                except ValueError as exc:
                    logging.info(f"{dev_mac}: device configuration invalid for "
                                 f"{device_class.__name__}: {exc}")
                    if not routed:
                        self._ignored_mac[dev_mac] = True
                        self._tap_ignored_macs.add(dev_mac)
                    continue
                except Exception:
                    logging.exception(f"{dev_mac}: unexpected error during device initialization")
                    if not routed:
                        self._ignored_mac[dev_mac] = True
                        self._tap_ignored_macs.add(dev_mac)
                    continue
            else:
                dev_instance = self._known_mac[dev_mac]

            now = time.monotonic()
            if now - self._last_adv_seen.get(dev_mac, 0) >= ADV_LOG_QUIET_PERIOD:
                logging.info(f"{dev_mac}: received manufacturer data: {man_data!r}")
            else:
                logging.debug(f"{dev_mac}: received manufacturer data: {man_data!r}")
            self._last_adv_seen[dev_mac] = now
            if dev_instance.check_manufacturer_data(man_data):
                dev_instance.handle_manufacturer_data(man_data)
            else:
                logging.info(f"{dev_mac}: ignoring manufacturer data due to data check")

    def _glib_process_tap(self, adv: TappedAdvertisement):
        """GLib idle callback — bridges from tap thread to main thread."""
        try:
            self._process_advertisement(adv.mac, adv.manufacturer_data,
                                        adv.adapter_index, adv.rssi)
        except Exception:
            logging.exception(f"Error processing tap advertisement from {adv.mac}")
        return False

    def _start_tap(self):
        """Start the HCI monitor tap in a background thread.

        The tap uses HCI_CHANNEL_MONITOR which sees ALL adapters (bound to
        HCI_DEV_NONE) — no need to wait for D-Bus adapter enumeration.
        """
        try:
            tap_sock = create_tap_socket()
        except OSError as exc:
            logging.error(f"Cannot open HCI monitor socket: {exc}")
            logging.error("No advertisement source available — service cannot function")
            return

        known_mfg_ids = self._known_mfg_ids
        last_mfg_data = self._last_mfg_data
        tap_seen = self._tap_seen_macs

        def _on_advertisement(adv: TappedAdvertisement):
            if not adv.manufacturer_data:
                return
            now = time.monotonic()
            self._last_tap_rx = now
            self._silence_warned = False
            mac = adv.mac
            tap_seen[mac] = now
            for mfg_id in adv.manufacturer_data:
                raw = adv.manufacturer_data[mfg_id]
                prev = last_mfg_data.get(mac)
                if prev is not None:
                    prev_data, prev_ts = prev
                    if prev_data == raw and now - prev_ts < DEDUP_KEEPALIVE_SECONDS:
                        return
                last_mfg_data[mac] = (raw, now)
                GLib.idle_add(self._glib_process_tap, adv)
                return

        def _tap_thread():
            try:
                run_tap_loop(tap_sock, _on_advertisement, self._tap_stop,
                             mfg_filter=known_mfg_ids,
                             ignored_macs=self._tap_ignored_macs)
            except Exception:
                logging.exception("HCI monitor tap thread died")

        self._tap_stop.clear()
        t = threading.Thread(target=_tap_thread, daemon=True, name="hci-monitor-tap")
        t.start()
        self._tap_thread = t
        self._last_tap_rx = time.monotonic()
        logging.info("HCI monitor tap started")

    def start(self):
        """Start the service: open the tap immediately, begin pruning timer."""
        self._start_tap()
        self._router.start()
        GLib.timeout_add_seconds(30, self._prune_tick)

    def _on_registrations_changed(self):
        """Called by the router when external registrations change.

        Mutates the tap manufacturer-ID filter in place (the tap thread holds
        a reference to the same set object) and clears MACs from the
        suppression lists when a new MAC-level registration matches them.
        """
        external_ids = self._router.get_registered_mfg_ids()
        new_ids = self._internal_mfg_ids | external_ids
        self._known_mfg_ids.update(new_ids)
        stale = self._known_mfg_ids - new_ids
        if stale:
            self._known_mfg_ids.difference_update(stale)
        logging.info("Tap mfg filter updated: %d IDs (%d internal + %d external)",
                     len(self._known_mfg_ids), len(self._internal_mfg_ids),
                     len(external_ids))

        registered_macs = self._router.get_registered_macs()
        if not registered_macs:
            return

        to_unsuppress: list[str] = []
        for mac in list(self._ignored_mac):
            if mac in registered_macs:
                to_unsuppress.append(mac)

        for mac in to_unsuppress:
            del self._ignored_mac[mac]
            self._tap_ignored_macs.discard(mac)
            self._last_mfg_data.pop(mac, None)

        if to_unsuppress:
            logging.info("Unsuppressed %d MAC(s) due to new MAC registrations", len(to_unsuppress))

    def _prune_tick(self):
        """GLib timer callback — prune caches, check tap health."""
        # Refresh TTLs for devices the tap thread has seen since last tick,
        # even if their data was deduplicated and not forwarded to _process_advertisement.
        seen = self._tap_seen_macs
        for mac in list(seen):
            if mac in self._known_mac:
                _ = self._known_mac[mac]  # __getitem__ refreshes TTL

        self._known_mac.prune()
        self._ignored_mac.prune()

        # Sync tap-level MAC filter: remove entries that expired from
        # _ignored_mac or were promoted to _known_mac.
        stale_ignored = [
            mac for mac in self._tap_ignored_macs
            if mac not in self._ignored_mac or mac in self._known_mac
        ]
        for mac in stale_ignored:
            self._tap_ignored_macs.discard(mac)

        now = time.monotonic()

        # Prune stale entries from dedup and log-throttle dicts
        stale_macs = [
            mac for mac, ts in self._last_adv_seen.items()
            if now - ts > DEVICE_SERVICES_TIMEOUT
        ]
        for mac in stale_macs:
            self._last_adv_seen.pop(mac, None)
            self._last_mfg_data.pop(mac, None)

        # Tap thread watchdog: restart if it died
        if self._tap_thread is not None and not self._tap_thread.is_alive():
            logging.warning("HCI monitor tap thread is dead — restarting")
            self._tap_thread = None
            self._start_tap()

        # Re-register monitors on any adapter that lost its registration
        for name in self._adapter_paths:
            if name not in self._registered_adapters:
                self._register_passive_monitor(name)

        # Silence detection: re-register passive monitors if no ads for 5 min
        if self._last_tap_rx > 0 and now - self._last_tap_rx > SILENCE_WARNING_SECONDS:
            if not self._silence_warned:
                logging.warning(
                    f"No matching advertisements received for "
                    f"{int(now - self._last_tap_rx)}s — re-registering passive scan")
                self._registered_adapters.clear()
                self._silence_warned = True

        return True

    def snif_data(self, man_id: int, man_data: bytes):
        man_name = MAN_NAMES.get(man_id, hex(man_id).upper())
        SNIF_LOGGER.info(f"{man_name!r}: {man_data!r}")

class DatedDict(MutableMapping):
    """
    Dict keeping timestamps for each entries so that older ones can be purged.
    Refreshes timestamp on read. Manual pruning required.
    """

    def __init__(self, ttl):
        self.ttl = ttl
        self._store = {}

    def _now(self): return time.monotonic()

    def __setitem__(self, key, value):
        self._store[key] = (value, self._now() + self.ttl)

    def __getitem__(self, key):
        value, _ = self._store[key]
        self._store[key] = (value, self._now() + self.ttl)
        return value

    def __delitem__(self, key):
        del self._store[key]

    def __iter__(self):
        return iter(self._store.keys())

    def __len__(self):
        return len(self._store)

    def __contains__(self, key):
        contains = key in self._store
        if contains:
            self[key]
        return contains

    def prune(self):
        now = self._now()
        for key in list(self._store.keys()):
            value, expire_time = self._store[key]
            if expire_time <= now:
                if getattr(value, 'delete', None):
                    value.delete()
                del self._store[key]

    def keys(self):
        return self._store.keys()

def main():
    parser = ArgumentParser(description=sys.argv[0])
    parser.add_argument('--version', '-v', action='version', version=PROCESS_VERSION)
    parser.add_argument('--debug', '-d', help='Turn on debug logging', default=False, action='store_true')
    parser.add_argument('--snif', '-s', help='Turn on advertising data sniffer', default=False, action='store_true')
    args = parser.parse_args()

    setup_logging(args.debug)

    if args.snif:
        handler = RotatingFileHandler(
            "/var/log/dbus-ble-sensors-py/sniffer.log",
            maxBytes=512 * 1024,
            backupCount=0,
            encoding="utf-8",
            delay=True
        )
        handler.setFormatter(logging.Formatter(fmt='%(message)s'))
        SNIF_LOGGER.addHandler(handler)

    # Immediate exit on SIGTERM so the OS closes all file descriptors and
    # the D-Bus daemon detects the disconnect cleanly.
    import signal
    signal.signal(signal.SIGTERM, lambda signum, frame: os._exit(0))

    DBusGMainLoop(set_as_default=True)

    service = DbusBleSensors()
    service.start()

    logging.info('Starting service')
    GLib.MainLoop().run()

if __name__ == "__main__":
    main()
