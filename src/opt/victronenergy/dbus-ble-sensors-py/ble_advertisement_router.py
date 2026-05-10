"""
BLE Advertisement Router

Routes BLE advertisements to external D-Bus services that register interest
via introspectable object paths.  Drop-in replacement for the standalone
dbus-ble-advertisements project -- identical registration paths, identical
signal interface and signature.

External services register by exposing introspectable D-Bus objects at:
  /ble_advertisements/{service}/mfgr/{id}
  /ble_advertisements/{service}/mfgr_product/{mfg}_{pid}
  /ble_advertisements/{service}/mfgr_product_range/{mfg}_{min}_{max}
  /ble_advertisements/{service}/addr/{mac}

The router emits Advertisement signals on those same paths.  Multiple
services can register for the same manufacturer ID and each receives its
own copy of the signal.
"""

import logging
import re
import struct
import time
import xml.etree.ElementTree as ET

import dbus
import dbus.service
from gi.repository import GLib

log = logging.getLogger(__name__)

ROUTER_INTERFACE = 'com.victronenergy.switch.ble_advertisements'
ROUTER_BUS_NAME = 'com.victronenergy.switch.ble_advertisements'
ROUTER_ROOT_PATH = '/ble_advertisements'

# Status returned by GetStatus() when the heartbeat is older than this.
# Matches the standalone dbus-ble-advertisements service so existing
# clients gating on GetStatus() see consistent behaviour.
HEARTBEAT_STALE_AFTER_SECONDS = 1800


def _tap_mac_to_colon(mac: str) -> str:
    """Convert lowercase no-separator MAC to uppercase colon-separated.

    '00a0508d9569' -> '00:A0:50:8D:95:69'
    """
    upper = mac.upper()
    return ':'.join(upper[i:i + 2] for i in range(0, 12, 2))


class AdvertisementEmitter(dbus.service.Object):
    """D-Bus object that emits Advertisement signals on a registration path."""

    @dbus.service.signal(dbus_interface=ROUTER_INTERFACE, signature='sqaynss')
    def Advertisement(self, mac, manufacturer_id, data, rssi, interface, name):
        pass


class _RootObject(dbus.service.Object):
    """Service-presence object at /ble_advertisements.

    Mirrors the standalone dbus-ble-advertisements surface so existing
    clients that gate on GetVersion()/GetStatus()/GetHeartbeat() before
    they start keep working when this in-process router takes over the
    bus name.
    """

    def __init__(self, bus_name, version: str):
        super().__init__(bus_name, ROUTER_ROOT_PATH)
        self._version = version
        self._heartbeat = time.time()

    def update_heartbeat(self):
        self._heartbeat = time.time()

    @dbus.service.method(dbus_interface=ROUTER_INTERFACE,
                         in_signature='', out_signature='s')
    def GetVersion(self):
        return self._version

    @dbus.service.method(dbus_interface=ROUTER_INTERFACE,
                         in_signature='', out_signature='s')
    def GetStatus(self):
        if time.time() - self._heartbeat < HEARTBEAT_STALE_AFTER_SECONDS:
            return "running"
        return "stale"

    @dbus.service.method(dbus_interface=ROUTER_INTERFACE,
                         in_signature='', out_signature='d')
    def GetHeartbeat(self):
        return self._heartbeat


class BleAdvertisementRouter:
    """Registration discovery, advertisement matching, and D-Bus signal emission.

    Scans all ``com.victronenergy.*`` services for introspectable paths under
    ``/ble_advertisements/`` and builds indexes so that incoming HCI tap
    advertisements can be matched and emitted as D-Bus signals to the
    appropriate consumers.

    Parameters
    ----------
    bus : dbus.bus.BusConnection
        System bus connection.
    version : str
        Service version string returned by GetVersion() on the root object.
    on_registrations_changed : callable or None
        Called (with no arguments) whenever registrations are added or
        removed, so the caller can update the HCI tap manufacturer-ID
        filter and clear suppressed MACs.
    """

    def __init__(self, bus, version: str, on_registrations_changed=None):
        self._bus = bus
        self._on_registrations_changed = on_registrations_changed

        self._bus_name = dbus.service.BusName(ROUTER_BUS_NAME, bus)
        self._root = _RootObject(self._bus_name, version)

        # Registration indexes -- values are sets of full object paths
        self._mfg_registrations: dict[int, set[str]] = {}
        self._mac_registrations: dict[str, set[str]] = {}
        self._pid_registrations: dict[tuple[int, int], set[str]] = {}
        self._pid_range_registrations: dict[tuple[int, int, int], set[str]] = {}

        # Emitters keyed by full object path
        self._emitters: dict[str, AdvertisementEmitter] = {}

        # Async initial-scan queue
        self._pending_scan_services: list[str] = []

        # Subscribe to service appear/disappear
        self._bus.add_signal_receiver(
            self._on_name_owner_changed,
            signal_name='NameOwnerChanged',
            dbus_interface='org.freedesktop.DBus',
            path='/org/freedesktop/DBus',
        )

        log.info("BLE advertisement router initialized")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self):
        """Kick off the async initial registration scan."""
        self._schedule_initial_scan()

    def process_advertisement(self, tap_mac: str, mfg_id: int, data: bytes,
                              rssi: int, interface: str) -> bool:
        """Match an advertisement against registrations and emit signals.

        Parameters
        ----------
        tap_mac : str
            MAC from the HCI tap (lowercase, no separators).
        mfg_id : int
            Manufacturer company ID.
        data : bytes
            Manufacturer-specific payload (without the company ID prefix).
        rssi : int
            RSSI value.
        interface : str
            Adapter name (e.g. ``hci0``).

        Returns
        -------
        bool
            True if the advertisement was emitted to at least one consumer.
        """
        self._root.update_heartbeat()

        mac = _tap_mac_to_colon(tap_mac)

        if not self._should_process(mac, mfg_id):
            return False

        product_id = self._extract_product_id(data)
        if not self._has_registration(mac, mfg_id, product_id):
            return False

        return self._emit_advertisement(mac, mfg_id, data, rssi, interface)

    def get_registered_mfg_ids(self) -> set[int]:
        """Return the set of manufacturer IDs with active registrations."""
        ids: set[int] = set()
        ids.update(self._mfg_registrations.keys())
        for mfg_id, _pid in self._pid_registrations:
            ids.add(mfg_id)
        for mfg_id, _lo, _hi in self._pid_range_registrations:
            ids.add(mfg_id)
        return ids

    def get_registered_macs(self) -> set[str]:
        """Return the set of MACs with active registrations (tap format)."""
        result: set[str] = set()
        for mac in self._mac_registrations:
            result.add(mac.replace(':', '').lower())
        return result

    def has_registrations(self) -> bool:
        """True if any registrations exist."""
        return bool(
            self._mfg_registrations
            or self._mac_registrations
            or self._pid_registrations
            or self._pid_range_registrations
        )

    # ------------------------------------------------------------------
    # Matching helpers
    # ------------------------------------------------------------------

    def _should_process(self, mac: str, mfg_id: int) -> bool:
        """Quick pre-check (no product-ID extraction)."""
        if mfg_id in self._mfg_registrations:
            return True
        if mac in self._mac_registrations:
            return True
        for reg_mfg, _pid in self._pid_registrations:
            if reg_mfg == mfg_id:
                return True
        for reg_mfg, _lo, _hi in self._pid_range_registrations:
            if reg_mfg == mfg_id:
                return True
        return False

    def _has_registration(self, mac: str, mfg_id: int,
                          product_id: int | None) -> bool:
        """Full match including product-ID filtering."""
        if mac in self._mac_registrations:
            return True
        if product_id is not None:
            if (mfg_id, product_id) in self._pid_registrations:
                return True
            for (reg_mfg, lo, hi) in self._pid_range_registrations:
                if reg_mfg == mfg_id and lo <= product_id <= hi:
                    return True
        if mfg_id in self._mfg_registrations:
            return True
        return False

    @staticmethod
    def _extract_product_id(data: bytes) -> int | None:
        """Extract Victron-style product ID from bytes 2-3 (LE uint16)."""
        if len(data) >= 4:
            try:
                return struct.unpack('<H', data[2:4])[0]
            except struct.error:
                pass
        return None

    # ------------------------------------------------------------------
    # Signal emission
    # ------------------------------------------------------------------

    def _emit_advertisement(self, mac: str, mfg_id: int, data: bytes,
                            rssi: int, interface: str) -> bool:
        """Emit D-Bus signals to all matching registration paths.

        Returns True if at least one signal was emitted.
        """
        product_id = self._extract_product_id(data)

        data_array = dbus.Array(data, signature='y')
        mac_dbus = dbus.String(mac)
        mfg_dbus = dbus.UInt16(mfg_id)
        rssi_dbus = dbus.Int16(rssi)
        iface_dbus = dbus.String(interface)
        name_dbus = dbus.String('')

        emitted = 0

        if mfg_id in self._mfg_registrations:
            for path in self._mfg_registrations[mfg_id]:
                if path in self._emitters:
                    self._emitters[path].Advertisement(
                        mac_dbus, mfg_dbus, data_array, rssi_dbus,
                        iface_dbus, name_dbus)
                    emitted += 1

        if product_id is not None:
            key = (mfg_id, product_id)
            if key in self._pid_registrations:
                for path in self._pid_registrations[key]:
                    if path in self._emitters:
                        self._emitters[path].Advertisement(
                            mac_dbus, mfg_dbus, data_array, rssi_dbus,
                            iface_dbus, name_dbus)
                        emitted += 1

            for (reg_mfg, lo, hi), paths in self._pid_range_registrations.items():
                if reg_mfg == mfg_id and lo <= product_id <= hi:
                    for path in paths:
                        if path in self._emitters:
                            self._emitters[path].Advertisement(
                                mac_dbus, mfg_dbus, data_array, rssi_dbus,
                                iface_dbus, name_dbus)
                            emitted += 1

        if mac in self._mac_registrations:
            for path in self._mac_registrations[mac]:
                if path in self._emitters:
                    self._emitters[path].Advertisement(
                        mac_dbus, mfg_dbus, data_array, rssi_dbus,
                        iface_dbus, name_dbus)
                    emitted += 1

        if emitted:
            log.debug("Routed %s mfg=%#06x to %d path(s)", mac, mfg_id, emitted)
        return emitted > 0

    # ------------------------------------------------------------------
    # Registration discovery
    # ------------------------------------------------------------------

    def _schedule_initial_scan(self):
        """Queue all com.victronenergy.* services for async introspection."""
        try:
            bus_obj = self._bus.get_object('org.freedesktop.DBus', '/')
            bus_iface = dbus.Interface(bus_obj, 'org.freedesktop.DBus')
            names = bus_iface.ListNames()

            self._pending_scan_services = [
                s for s in names
                if isinstance(s, str)
                and s.startswith('com.victronenergy.')
                and not s.startswith(':')
                and s != ROUTER_BUS_NAME
            ]
            log.info("Queued %d services for registration scan",
                     len(self._pending_scan_services))

            if self._pending_scan_services:
                GLib.timeout_add(100, self._scan_next_service)
        except Exception:
            log.exception("Error scheduling initial registration scan")

    def _scan_next_service(self):
        """Process one queued service, then reschedule if more remain."""
        if not self._pending_scan_services:
            log.info("Registration scan complete: mfgr=%d mac=%d pid=%d range=%d",
                     len(self._mfg_registrations), len(self._mac_registrations),
                     len(self._pid_registrations), len(self._pid_range_registrations))
            return False

        service = self._pending_scan_services.pop(0)
        try:
            self._check_service_registrations(service)
        except Exception:
            log.debug("Error checking %s", service, exc_info=True)

        if self._pending_scan_services:
            GLib.timeout_add(100, self._scan_next_service)
        else:
            log.info("Registration scan complete: mfgr=%d mac=%d pid=%d range=%d",
                     len(self._mfg_registrations), len(self._mac_registrations),
                     len(self._pid_registrations), len(self._pid_range_registrations))
        return False

    def _on_name_owner_changed(self, name, old_owner, new_owner):
        if name.startswith(':') or name == ROUTER_BUS_NAME:
            return
        if new_owner and not old_owner:
            self._check_service_registrations(name)
        elif old_owner and not new_owner:
            self._remove_service_registrations(name)

    def _check_service_registrations(self, service_name, timeout=1.0):
        if not service_name.startswith('com.victronenergy.'):
            return
        try:
            obj = self._bus.get_object(service_name, '/')
            intro = dbus.Interface(obj, 'org.freedesktop.DBus.Introspectable')
            intro.Introspect(
                reply_handler=lambda xml: self._on_introspect_reply(service_name, xml),
                error_handler=lambda e: self._on_introspect_error(service_name, e),
                timeout=timeout,
            )
        except Exception:
            log.debug("Could not introspect %s", service_name, exc_info=True)

    def _on_introspect_reply(self, service_name, xml):
        try:
            if 'ble_advertisements' not in xml:
                return
            log.info("Service %s has ble_advertisements paths, parsing", service_name)
            self._parse_registrations(service_name, '/', xml)
            self._update_emitters()
            self._notify_registrations_changed()
        except Exception:
            log.debug("Error processing introspection for %s", service_name, exc_info=True)

    def _on_introspect_error(self, service_name, error):
        log.debug("Introspection of %s failed: %s", service_name, error)

    # ------------------------------------------------------------------
    # Registration parsing
    # ------------------------------------------------------------------

    def _parse_registrations(self, service_name: str, path: str, xml: str):
        """Recursively parse introspection XML for registration paths."""
        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            log.debug("Bad XML from %s at %s", service_name, path)
            return

        if '/ble_advertisements/' in path:
            if '/mfgr_product_range/' in path:
                m = re.search(r'/mfgr_product_range/(\d+)_(\d+)_(\d+)$', path)
                if m:
                    mfg = int(m.group(1))
                    lo = int(m.group(2))
                    hi = int(m.group(3))
                    key = (mfg, lo, hi)
                    self._pid_range_registrations.setdefault(key, set()).add(path)
                    log.info("Registered range %s (mfg=%d pid=%d-%d)", path, mfg, lo, hi)

            elif '/mfgr_product/' in path:
                m = re.search(r'/mfgr_product/(\d+)_(\d+)$', path)
                if m:
                    mfg = int(m.group(1))
                    pid = int(m.group(2))
                    key = (mfg, pid)
                    self._pid_registrations.setdefault(key, set()).add(path)
                    log.info("Registered product %s (mfg=%d pid=%d)", path, mfg, pid)

            elif '/mfgr/' in path:
                m = re.search(r'/mfgr/(\d+)$', path)
                if m:
                    mfg = int(m.group(1))
                    self._mfg_registrations.setdefault(mfg, set()).add(path)
                    log.info("Registered mfgr %s (mfg=%d)", path, mfg)

            elif '/addr/' in path:
                parts = path.split('/addr/')
                if len(parts) == 2:
                    mac_part = parts[1].replace('_', '')
                    if ':' not in mac_part and len(mac_part) == 12:
                        mac_str = ':'.join(mac_part[i:i + 2] for i in range(0, 12, 2))
                    else:
                        mac_str = mac_part
                    mac_str = mac_str.upper()
                    self._mac_registrations.setdefault(mac_str, set()).add(path)
                    log.info("Registered addr %s (MAC=%s)", path, mac_str)

        for node in root.findall('node'):
            child_name = node.get('name')
            if not child_name:
                continue
            child_path = f"{path}/{child_name}".replace('//', '/')
            try:
                obj = self._bus.get_object(service_name, child_path)
                intro = dbus.Interface(obj, 'org.freedesktop.DBus.Introspectable')
                child_xml = intro.Introspect()
                self._parse_registrations(service_name, child_path, child_xml)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Registration removal
    # ------------------------------------------------------------------

    def _remove_service_registrations(self, service_name):
        """Remove all registrations and emitters for a disappeared service."""
        removed = 0

        for collection in (self._mfg_registrations,
                           self._mac_registrations,
                           self._pid_registrations,
                           self._pid_range_registrations):
            for key, paths in list(collection.items()):
                to_remove = {p for p in paths if service_name in p}
                if to_remove:
                    paths.difference_update(to_remove)
                    removed += len(to_remove)
                    if not paths:
                        del collection[key]

        emitter_paths = [p for p in self._emitters if service_name in p]
        for path in emitter_paths:
            try:
                self._emitters[path].remove_from_connection()
            except Exception:
                pass
            del self._emitters[path]
            removed += 1

        if removed:
            log.info("Service %s disappeared, removed %d registration(s)", service_name, removed)
            self._notify_registrations_changed()

    # ------------------------------------------------------------------
    # Emitter management
    # ------------------------------------------------------------------

    def _update_emitters(self):
        """Sync emitters with active registration paths."""
        active: set[str] = set()
        for paths in self._mfg_registrations.values():
            active.update(paths)
        for paths in self._mac_registrations.values():
            active.update(paths)
        for paths in self._pid_registrations.values():
            active.update(paths)
        for paths in self._pid_range_registrations.values():
            active.update(paths)

        for path in active:
            if path not in self._emitters:
                try:
                    self._emitters[path] = AdvertisementEmitter(self._bus_name, path)
                    log.info("Created emitter at %s", path)
                except Exception:
                    log.error("Failed to create emitter at %s", path, exc_info=True)

        for path in list(self._emitters):
            if path not in active:
                try:
                    self._emitters[path].remove_from_connection()
                except Exception:
                    pass
                del self._emitters[path]
                log.info("Removed emitter at %s", path)

    # ------------------------------------------------------------------
    # Change notification
    # ------------------------------------------------------------------

    def _notify_registrations_changed(self):
        if self._on_registrations_changed is not None:
            try:
                self._on_registrations_changed()
            except Exception:
                log.exception("Error in on_registrations_changed callback")
