#!/usr/bin/env python3
import sys
import os
sys.path.insert(1, os.path.join(os.path.dirname(__file__), 'ext'))
sys.path.insert(1, os.path.join(os.path.dirname(__file__), 'ext', 'velib_python'))
import logging
from logging.handlers import RotatingFileHandler
import asyncio
import dbus
from dbus.mainloop.glib import DBusGMainLoop
from argparse import ArgumentParser
from ble_device import BleDevice
from ble_role import BleRole
from dbus_bus import get_bus
from dbus_ble_service import DbusBleService
import bleak
from bleak.assigned_numbers import AdvertisementDataType
import gbulb
from logger import setup_logging
from collections.abc import MutableMapping
import threading
import time
from conf import IGNORED_DEVICES_TIMEOUT, DEVICE_SERVICES_TIMEOUT, PROCESS_VERSION

ADV_LOG_QUIET_PERIOD = 1800  # seconds before re-logging a known device's advertisements
from man_id import MAN_NAMES

SNIF_LOGGER = logging.getLogger("sniffer")
SNIF_LOGGER.propagate = False

SCANNER_INITIAL_BACKOFF = 2
SCANNER_MAX_BACKOFF = 60
SCANNER_MAX_PASSIVE_RETRIES = 5
SCANNER_ACTIVE_CYCLE_DURATION = 15

# OrPattern tuples for passive scanning via BlueZ AdvertisementMonitor1.
# Passive scanning avoids calling StartDiscovery, which eliminates scan
# contention (InProgress errors) when multiple BLE services share the
# adapter.  Each tuple is (offset, AD_type, value) where AD_type is the
# BLE AD type code and value is a prefix to match.
# Requires BlueZ >= 5.56 with --experimental and kernel >= 5.10.
#
# Only Flags-based patterns are used here.  Manufacturer-data patterns
# (AD type 0xFF) crash BlueZ 5.72 / kernel 6.12 with heap corruption
# (free(): invalid next size).  To catch devices that omit Flags entirely
# (e.g. Mopeka sensors), a periodic active scan with DuplicateData=True
# is run alongside the passive scanner — see _run_nondiscoverable_scan().
PASSIVE_SCAN_OR_PATTERNS = [
    (0, AdvertisementDataType.FLAGS, bytes([0x02])),
    (0, AdvertisementDataType.FLAGS, bytes([0x04])),
    (0, AdvertisementDataType.FLAGS, bytes([0x05])),
    (0, AdvertisementDataType.FLAGS, bytes([0x06])),
    (0, AdvertisementDataType.FLAGS, bytes([0x0e])),
    (0, AdvertisementDataType.FLAGS, bytes([0x1a])),
    (0, AdvertisementDataType.FLAGS, bytes([0x1e])),
]

NONDISCOVERABLE_SCAN_INTERVAL = 30
NONDISCOVERABLE_SCAN_DURATION = 15

class DbusBleSensors(object):
    """
    Main class for the D-bus BLE Sensors python service.
    Extends base C service 'dbus-ble-sensors' to allow community integration of any BLE sensors.

    Cf.
    - https://github.com/victronenergy/dbus-ble-sensors/
    - https://github.com/victronenergy/node-red-contrib-victron/blob/master/src/nodes/victron-virtual.js
    - https://github.com/victronenergy/gui-v2/blob/main/data/mock/conf/services/ruuvi-salon.json

    TODO: Handle ve item format using units definition on GetText callbacks ?
    """

    def __init__(self):
        self._dbus: dbus.bus.BusConnection = get_bus("org.bluez")
        # Accessor to dbus ble dedicated service (default : com.victronenergy.ble)
        self._dbus_ble_service = DbusBleService()

        # Initialze BT adapters search
        self._adapters = []
        self._list_adapters()

        # Known device lists
        self._known_mac = DatedDict(ttl=DEVICE_SERVICES_TIMEOUT)
        self._ignored_mac = DatedDict(ttl=IGNORED_DEVICES_TIMEOUT)
        self._last_adv_seen: dict[str, float] = {}

        # Load definition classes
        BleRole.load_classes(os.path.abspath(__file__))
        BleDevice.load_classes(os.path.abspath(__file__))

    def _list_adapters(self):
        # Adding callback for future connections/disconnections
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

        # Initial search for adapters
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
                self._dbus_ble_service.add_ble_adapter(name, mac)

    def _on_interfaces_removed(self, path, interfaces):
        if not str(path).startswith('/org/bluez'):
            return
        name = path.split('/')[-1]
        if 'org.bluez.Adapter1' in interfaces:
            # Remove adapter
            self._dbus_ble_service.remove_ble_adapter(name)
            self._adapters.remove(name)
            logging.info(f"{name}: adapter removed")

    def _process_advertisement(self, device, advertisement_data):
        """Process a single BLE advertisement (called from scan_loop on the main thread)."""
        dev_mac = "".join(device.address.split(':')).lower()
        if dev_mac in self._ignored_mac:
            return

        plog = f"{dev_mac} - {device.name}:"
        logging.debug(f"{plog} received advertisement {advertisement_data!r}")
        if advertisement_data.manufacturer_data is None or len(advertisement_data.manufacturer_data) < 1:
            now = time.monotonic()
            if now - self._last_adv_seen.get(dev_mac, 0) >= ADV_LOG_QUIET_PERIOD:
                logging.info(f"{plog} ignoring, device without manufacturer data")
            self._last_adv_seen[dev_mac] = now
            self._ignored_mac[dev_mac] = True
            return

        for man_id, man_data in advertisement_data.manufacturer_data.items():
            if dev_mac not in self._known_mac:
                self.snif_data(man_id, man_data)

                device_class = BleDevice.DEVICE_CLASSES.get(man_id, None)
                if device_class is None:
                    now = time.monotonic()
                    if now - self._last_adv_seen.get(dev_mac, 0) >= ADV_LOG_QUIET_PERIOD:
                        logging.info(f"{plog} ignoring data {man_data!r}, no device configuration class for manufacturer {man_id!r}")
                    self._last_adv_seen[dev_mac] = now
                    self._ignored_mac[dev_mac] = True
                    continue

                logging.info(f"{plog} initializing device with class {device_class}")
                try:
                    dev_instance = device_class(dev_mac)
                    if not dev_instance.check_manufacturer_data(man_data):
                        raise ValueError(f"{plog} ignoring data {man_data!r}, manufacturer data check failed")
                    dev_instance.configure(man_data)
                    dev_instance.init()
                    self._known_mac[dev_mac] = dev_instance
                except Exception as e:
                    logging.exception(f"{plog} ignoring data {man_data!r}, an error occurred during device initialization:")
                    self._ignored_mac[dev_mac] = True
                    continue
            else:
                dev_instance = self._known_mac[dev_mac]

            now = time.monotonic()
            if now - self._last_adv_seen.get(dev_mac, 0) >= ADV_LOG_QUIET_PERIOD:
                logging.info(f"{plog} received manufacturer data: {man_data!r}")
            else:
                logging.debug(f"{plog} received manufacturer data: {man_data!r}")
            self._last_adv_seen[dev_mac] = now
            if dev_instance.check_manufacturer_data(man_data):
                dev_instance.handle_manufacturer_data(man_data)
            else:
                logging.info(f"{plog} ignoring manufacturer data due to data check")

    def _start_scanners(self):
        """Start BLE scanners in background threads.

        Passive scanners and nondiscoverable (DuplicateData) scanners run in
        separate threads, each with its own asyncio event loop.  This is
        necessary because:

        1. The main thread uses gbulb (GLib-backed asyncio) which is
           incompatible with dbus-fast's method-call dispatch needed for
           passive scanning via AdvertisementMonitor1.

        2. If passive scanning's AdvertisementMonitor1 callbacks fail (e.g.
           method signature mismatch with BlueZ), the dbus-fast connection
           can block the event loop, preventing nondiscoverable scans from
           starting.  Separate threads with separate event loops isolate
           the two scanning modes from each other."""
        self._scan_buffer = []
        self._scan_buffer_lock = threading.Lock()
        self._scanner_stop = threading.Event()

        def _make_thread(coro_factory, name):
            def _thread_main():
                asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    loop.run_until_complete(coro_factory())
                except Exception:
                    logging.exception(f"{name} thread error")
            t = threading.Thread(target=_thread_main, daemon=True, name=name)
            t.start()
            return t

        self._scanner_thread = _make_thread(
            self._run_scanners, "passive-scanner")
        self._nd_scanner_thread = _make_thread(
            self._run_nondiscoverable_scans, "nondiscoverable-scanner")

    @staticmethod
    async def _power_cycle_adapter(adapter):
        """Power-cycle a BlueZ adapter to clear corrupted HCI scan state.

        When the C-based dbus-ble-sensors service uses raw HCI sockets with
        legacy scan commands (0x200B/0x200C) on a BT 5.x adapter, BlueZ
        cannot disable the orphaned scan because it sends extended commands
        (0x2041/0x2042) which the controller rejects (Command Disallowed /
        EBUSY).  Power-cycling via D-Bus triggers a proper HCI Reset that
        clears all controller state."""
        from dbus_fast.aio import MessageBus
        from dbus_fast import BusType, Variant

        adapter_path = f'/org/bluez/{adapter}'
        logging.info(f"{adapter}: power-cycling to clear HCI state")
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        try:
            introspection = await bus.introspect('org.bluez', adapter_path)
            proxy = bus.get_proxy_object('org.bluez', adapter_path, introspection)
            props = proxy.get_interface('org.freedesktop.DBus.Properties')
            await props.call_set('org.bluez.Adapter1', 'Powered', Variant('b', False))
            await asyncio.sleep(1)
            await props.call_set('org.bluez.Adapter1', 'Powered', Variant('b', True))
            await asyncio.sleep(2)
            logging.info(f"{adapter}: power-cycle complete")
        except Exception as e:
            logging.warning(f"{adapter}: power-cycle failed: {e}")
        finally:
            bus.disconnect()

    async def _run_scanners(self):
        """Run persistent BleakScanners with per-adapter state tracking.

        Each adapter starts in passive mode.  On failure, retries with
        exponential backoff up to MAX_PASSIVE_RETRIES times.  After that,
        power-cycles the adapter via D-Bus to clear any corrupted HCI scan
        state (e.g. from the C-based dbus-ble-sensors service using raw HCI
        sockets with legacy commands on a BT 5.x adapter).  If passive
        scanning still fails after the power-cycle, falls back to one
        active scan cycle before returning to passive.
        Mode transitions are logged at INFO level per adapter."""
        INITIAL_BACKOFF = SCANNER_INITIAL_BACKOFF
        MAX_BACKOFF = SCANNER_MAX_BACKOFF
        MAX_PASSIVE_RETRIES = SCANNER_MAX_PASSIVE_RETRIES
        ACTIVE_CYCLE_DURATION = SCANNER_ACTIVE_CYCLE_DURATION

        def _detection_callback(device, adv_data):
            with self._scan_buffer_lock:
                self._scan_buffer.append((device, adv_data))

        adapter_state = {}

        def _state(adapter):
            if adapter not in adapter_state:
                adapter_state[adapter] = {
                    'mode': 'passive',
                    'scanner': None,
                    'retries': 0,
                    'backoff': INITIAL_BACKOFF,
                    'retry_at': 0.0,
                    'logged_mode': None,
                    'power_cycled': False,
                }
            return adapter_state[adapter]

        def _log_mode(adapter, st, reason=''):
            if st['logged_mode'] != st['mode']:
                suffix = f' ({reason})' if reason else ''
                logging.info(f"{adapter}: scanning mode: {st['mode']}{suffix}")
                st['logged_mode'] = st['mode']

        while not self._scanner_stop.is_set():
            now = asyncio.get_event_loop().time()

            for adapter in self._adapters:
                st = _state(adapter)

                if st['scanner'] is not None:
                    continue
                if now < st['retry_at']:
                    continue

                if st['mode'] == 'passive':
                    try:
                        scanner = bleak.BleakScanner(
                            detection_callback=_detection_callback,
                            scanning_mode='passive',
                            bluez=dict(or_patterns=PASSIVE_SCAN_OR_PATTERNS, adapter=adapter),
                        )
                        await scanner.start()
                        st['scanner'] = scanner
                        st['retries'] = 0
                        st['backoff'] = INITIAL_BACKOFF
                        _log_mode(adapter, st)
                    except Exception as e:
                        st['retries'] += 1
                        delay = st['backoff']
                        st['backoff'] = min(delay * 2, MAX_BACKOFF)
                        st['retry_at'] = now + delay

                        if st['retries'] >= MAX_PASSIVE_RETRIES:
                            if not st['power_cycled']:
                                logging.warning(
                                    f"{adapter}: passive scan failed {st['retries']} times, "
                                    f"power-cycling adapter to clear HCI state"
                                )
                                await self._power_cycle_adapter(adapter)
                                st['power_cycled'] = True
                                st['retries'] = 0
                                st['backoff'] = INITIAL_BACKOFF
                                st['retry_at'] = 0.0
                                st['logged_mode'] = None
                            else:
                                logging.warning(
                                    f"{adapter}: passive scan still failing after power-cycle, "
                                    f"falling back to active for one cycle"
                                )
                                st['mode'] = 'active'
                                st['retry_at'] = 0.0
                        else:
                            logging.warning(
                                f"{adapter}: passive scan start failed ({e}), "
                                f"retry {st['retries']}/{MAX_PASSIVE_RETRIES} in {delay:.0f}s"
                            )

                elif st['mode'] == 'active':
                    try:
                        scanner = bleak.BleakScanner(
                            detection_callback=_detection_callback,
                            bluez=dict(adapter=adapter),
                        )
                        await scanner.start()
                        _log_mode(adapter, st, reason='fallback')

                        for _ in range(ACTIVE_CYCLE_DURATION):
                            if self._scanner_stop.is_set():
                                break
                            await asyncio.sleep(1)

                        await scanner.stop()
                    except bleak.exc.BleakDBusError as e:
                        if "InProgress" in str(e):
                            logging.debug(f"{adapter}: active scan InProgress, will retry")
                        else:
                            logging.warning(f"{adapter}: active scan error: {e}")
                    except Exception as e:
                        logging.warning(f"{adapter}: active scan error: {e}")

                    st['mode'] = 'passive'
                    st['retries'] = 0
                    st['backoff'] = INITIAL_BACKOFF
                    st['retry_at'] = 0.0
                    st['power_cycled'] = False
                    logging.info(f"{adapter}: active cycle complete, retrying passive")

            await asyncio.sleep(1)

        for adapter, st in adapter_state.items():
            if st['scanner'] is not None:
                try:
                    await st['scanner'].stop()
                    logging.info(f"{adapter}: scanner stopped")
                except Exception:
                    pass

    async def _run_nondiscoverable_scans(self):
        """Periodic active scans with DuplicateData=True to detect devices
        that omit the Flags AD type (e.g. Mopeka sensors advertise only
        manufacturer data under Nordic's company ID 0x0059 with no Flags).

        These scans run alongside the persistent passive scanners.  If the
        adapter is busy (InProgress), the scan is silently skipped."""
        while not self._scanner_stop.is_set():
            await asyncio.sleep(NONDISCOVERABLE_SCAN_INTERVAL)
            for adapter in list(self._adapters):
                try:
                    nd_results = []

                    def _nd_callback(device, adv_data):
                        nd_results.append((device, adv_data))

                    scanner = bleak.BleakScanner(
                        detection_callback=_nd_callback,
                        bluez=dict(
                            adapter=adapter,
                            filters={"DuplicateData": True},
                        ),
                    )
                    await scanner.start()
                    for _ in range(NONDISCOVERABLE_SCAN_DURATION):
                        if self._scanner_stop.is_set():
                            break
                        await asyncio.sleep(1)
                    await scanner.stop()

                    with self._scan_buffer_lock:
                        self._scan_buffer.extend(nd_results)
                    logging.info(
                        f"{adapter}: nondiscoverable scan finished "
                        f"({len(nd_results)} advertisements)"
                    )
                except bleak.exc.BleakDBusError as e:
                    if "InProgress" in str(e):
                        logging.debug(
                            f"{adapter}: nondiscoverable scan skipped (InProgress)"
                        )
                    else:
                        logging.warning(
                            f"{adapter}: nondiscoverable scan error: {e}"
                        )
                except Exception as e:
                    logging.warning(
                        f"{adapter}: nondiscoverable scan error: {e}"
                    )

    async def scan_loop(self):
        DRAIN_INTERVAL = 5

        while True:
            if len(self._adapters) < 1:
                logging.warning("Waiting for a bluetooth adapter...")
                await asyncio.sleep(5)
                continue

            if not hasattr(self, '_scanner_thread'):
                self._start_scanners()
                await asyncio.sleep(DRAIN_INTERVAL)

            with self._scan_buffer_lock:
                results = self._scan_buffer[:]
                self._scan_buffer.clear()

            for device, ad_data in results:
                try:
                    self._process_advertisement(device, ad_data)
                except Exception:
                    logging.exception(f"Error processing advertisement from {device.address}")

            self._known_mac.prune()
            self._ignored_mac.prune()

            await asyncio.sleep(DRAIN_INTERVAL)

    def snif_data(self, man_id: int, man_data: bytes):
        """
        Snif advertising data for given manufacturer id and data.
        Used for external sniffer mode.
        """
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
        # refresh on read
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
            self[key]   # refresh on check
        return contains

    def prune(self):
        now = self._now()
        for key in list(self._store.keys()):
            value, expire_time = self._store[key]
            if expire_time <= now:
                if getattr(value, 'delete', None):
                    value.delete()  # Destroy now, don't wait for GC
                del self._store[key]

    def keys(self):
        return self._store.keys()


def main():
    parser = ArgumentParser(description=sys.argv[0])
    parser.add_argument('--version', '-v', action='version', version=PROCESS_VERSION)
    parser.add_argument('--debug', '-d', help='Turn on debug logging', default=False, action='store_true')
    parser.add_argument('--snif', '-s', help='Turn on advertising data sniffer', default=False, action='store_true')
    args = parser.parse_args()

    # Set default logger
    setup_logging(args.debug)
    if args.debug:
        # Mute overly verbose libraries
        logging.getLogger("bleak").setLevel(logging.INFO)

    # Set sniffer logger
    if args.snif:
        handler = RotatingFileHandler(
            "/var/log/dbus-ble-sensors-py/sniffer.log",
            maxBytes=512 * 1024,  # rotate after 512KB
            backupCount=0,        # keep 5 rolled files: sniffer.log.1 ... .5
            encoding="utf-8",
            delay=True            # create file only on first emit
        )
        handler.setFormatter(logging.Formatter(fmt='%(message)s'))
        SNIF_LOGGER.addHandler(handler)

    # Init gbulb, configure GLib and integrate asyncio in it
    gbulb.install()
    DBusGMainLoop(set_as_default=True)
    asyncio.set_event_loop_policy(gbulb.GLibEventLoopPolicy())

    pvac_output = DbusBleSensors()

    mainloop = asyncio.new_event_loop()
    asyncio.set_event_loop(mainloop)
    asyncio.get_event_loop().create_task(pvac_output.scan_loop())
    logging.info('Starting service')
    mainloop.run_forever()


if __name__ == "__main__":
    main()
