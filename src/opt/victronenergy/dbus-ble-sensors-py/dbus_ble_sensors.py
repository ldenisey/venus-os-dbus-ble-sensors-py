#!/usr/bin/env python3
import sys
import os
sys.path.insert(1, os.path.join(os.path.dirname(__file__), 'ext'))
sys.path.insert(1, os.path.join(os.path.dirname(__file__), 'ext', 'velib_python'))
import logging
import asyncio
import dbus
from dbus.mainloop.glib import DBusGMainLoop
from argparse import ArgumentParser
from ble_device import BleDevice
from ble_role import BleRole
from dbus_ble_service import DbusBleService
import bleak
import gbulb
from logger import setup_logging
from collections.abc import MutableMapping
import time
from conf import SCAN_TIMEOUT, SCAN_SLEEP, IGNORED_DEVICES_TIMEOUT, DEVICE_SERVICES_TIMEOUT


class DbusBleSensors(object):
    """
    Main class for the D-bus BLE Sensors python service.
    Extends base C service 'dbus-ble-sensors' to allow community integration of any BLE sensors.

    Cf.
    - https://github.com/victronenergy/dbus-ble-sensors/
    - https://github.com/victronenergy/node-red-contrib-victron/blob/master/src/nodes/victron-virtual.js
    - https://github.com/victronenergy/gui-v2/blob/main/data/mock/conf/services/ruuvi-salon.json

    TODO: Find Gobius ans SolarSense product ids
    TODO: Handle ve item format using units definition on GetText callbacks ?
    """

    def __init__(self):
        # Get dbus, default is system
        self._dbus: dbus.Bus = dbus.SessionBus() if 'DBUS_SESSION_BUS_ADDRESS' in os.environ else dbus.SystemBus()
        # Accessor to dbus ble dedicated service (default : com.victronenergy.ble)
        self._dbus_ble_service = DbusBleService()

        # Initialze BT adapters search
        self._adapters = []
        self._list_adapters()

        # Known device lists
        self._known_mac = DatedDict(ttl=DEVICE_SERVICES_TIMEOUT)
        self._ignored_mac = DatedDict(ttl=IGNORED_DEVICES_TIMEOUT)

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

    async def _scan(self, adapter: str):
        def _scan_callback(device, advertisement_data):
            dev_mac = "".join(device.address.split(':')).lower()
            if dev_mac in self._ignored_mac:
                # Ignoring devices already evaluated
                return

            plog = f"{dev_mac} - {device.name}:"
            logging.debug(f"{plog} received advertisement {advertisement_data!r}")
            if advertisement_data.manufacturer_data is None or len(advertisement_data.manufacturer_data) < 1:
                logging.info(f"{plog} ignoring, device without manufacturer data")
                self._ignored_mac[dev_mac] = True
                return

            # Loop through manufacturer data fields, even though most devices only use one
            for man_id, man_data in advertisement_data.manufacturer_data.items():
                if dev_mac not in self._known_mac:
                    device_class = BleDevice.DEVICE_CLASSES.get(man_id, None)
                    if device_class is None:
                        logging.info(f"{plog} ignoring, no device configuration class for manufacturer {man_id!r}")
                        self._ignored_mac[dev_mac] = True
                        continue

                    # Run device specific parsing
                    logging.info(f"{plog} initializing device with class {device_class}")
                    try:
                        dev_instance = device_class(dev_mac)
                        if not dev_instance.check_manufacturer_data(man_data):
                            raise ValueError(f"{plog} ignoring, manufacturer data check failed")
                        dev_instance.configure(man_data)
                        dev_instance.init()
                        self._known_mac[dev_mac] = dev_instance
                    except Exception as e:
                        logging.info(f"{plog} ignoring, an error occurred during device initialization: {e}")
                        self._ignored_mac[dev_mac] = True
                        continue
                else:
                    dev_instance = self._known_mac[dev_mac]

                # Parsing data
                logging.info(f"{plog} received manufacturer data: {man_data!r}")
                if dev_instance.check_manufacturer_data(man_data):
                    dev_instance.handle_manufacturer_data(man_data)
                else:
                    logging.info(f"{plog} ignoring manufacturer data due to data check")

        logging.debug(f"{adapter}: Scanning ...")
        try:
            async with bleak.BleakScanner(adapter=adapter, detection_callback=_scan_callback) as scanner:
                await asyncio.sleep(SCAN_TIMEOUT)
            logging.debug(f"{adapter}: Scan finished")
        except Exception:
            logging.exception(f"{adapter}: Scan error")

    async def scan_loop(self):
        while True:
            # Start scans on all adapters
            if len(self._adapters) < 1:
                logging.warning("Waiting for a bluetooth adapter...")
                await asyncio.sleep(5)
                continue
            scan_tasks = [asyncio.create_task(self._scan(adapter)) for adapter in self._adapters]
            await asyncio.gather(*scan_tasks)

            # Clean known/ignored device lists
            self._known_mac.prune()
            self._ignored_mac.prune()

            # Wait before next scan if needed
            if self._dbus_ble_service.get_continuous_scan():
                logging.debug(f"{self._adapters}: continuous scan on, restarting scan immediately")
            else:
                logging.debug(f"{self._adapters}: continuous scan off, pausing for {SCAN_SLEEP!r} seconds")
                await asyncio.sleep(SCAN_SLEEP)


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
    parser.add_argument('--debug', '-d', help='Turn on debug logging', default=False, action='store_true')
    args = parser.parse_args()
    setup_logging(args.debug)
    if args.debug:
        # Mute overly verbose libraries
        logging.getLogger("bleak").setLevel(logging.INFO)

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
