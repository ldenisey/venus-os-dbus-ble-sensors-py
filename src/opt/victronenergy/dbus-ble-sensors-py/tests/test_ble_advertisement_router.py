"""Unit tests for the BLE advertisement router.

Tests registration parsing, advertisement matching, product-ID extraction,
MAC format conversion, and the registrations-changed callback -- without
requiring a live D-Bus system bus.
"""

import struct
import sys
import unittest
from unittest.mock import MagicMock, patch, call

# Stub dbus and gi before importing the router so the module loads on
# any platform (macOS, CI, etc.)
class _FakeDbusObject:
    """Dummy stand-in for dbus.service.Object so subclasses can be
    instantiated under the test mock without tripping MagicMock's
    call-tracking machinery."""
    def __init__(self, *args, **kwargs):
        pass


def _fake_dbus_decorator(*args, **kwargs):
    """Stand-in for @dbus.service.method / .signal — returns the
    decorated function unchanged."""
    def _wrap(fn):
        return fn
    return _wrap


_dbus_service_mod = MagicMock()
_dbus_service_mod.Object = _FakeDbusObject
_dbus_service_mod.method = _fake_dbus_decorator
_dbus_service_mod.signal = _fake_dbus_decorator
_dbus_mod = MagicMock()
_dbus_mod.service = _dbus_service_mod
_dbus_mod.String = str
_dbus_mod.UInt16 = int
_dbus_mod.Int16 = int
_dbus_mod.Array = lambda data, signature=None: list(data)
sys.modules.setdefault('dbus', _dbus_mod)
sys.modules.setdefault('dbus.service', _dbus_service_mod)
sys.modules.setdefault('dbus.mainloop', MagicMock())
sys.modules.setdefault('dbus.mainloop.glib', MagicMock())

_gi_mod = MagicMock()
sys.modules.setdefault('gi', _gi_mod)
sys.modules.setdefault('gi.repository', MagicMock())

import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from ble_advertisement_router import (
    BleAdvertisementRouter,
    HEARTBEAT_STALE_AFTER_SECONDS,
    _tap_mac_to_colon,
)


class TestTapMacToColon(unittest.TestCase):

    def test_lowercase_to_uppercase_colon(self):
        self.assertEqual(_tap_mac_to_colon('00a0508d9569'), '00:A0:50:8D:95:69')

    def test_all_zeros(self):
        self.assertEqual(_tap_mac_to_colon('000000000000'), '00:00:00:00:00:00')

    def test_all_ff(self):
        self.assertEqual(_tap_mac_to_colon('ffffffffffff'), 'FF:FF:FF:FF:FF:FF')

    def test_mixed_case_input(self):
        self.assertEqual(_tap_mac_to_colon('aAbBcCdDeEfF'), 'AA:BB:CC:DD:EE:FF')


class TestExtractProductId(unittest.TestCase):

    def setUp(self):
        bus = MagicMock()
        bus.add_signal_receiver = MagicMock()
        self.router = BleAdvertisementRouter(bus, version='1.0.0')

    def test_valid_payload(self):
        data = b'\x10\x02' + struct.pack('<H', 0xA389) + b'\x00\x00'
        self.assertEqual(self.router._extract_product_id(data), 0xA389)

    def test_short_payload_returns_none(self):
        self.assertIsNone(self.router._extract_product_id(b'\x01\x02\x03'))

    def test_empty_payload_returns_none(self):
        self.assertIsNone(self.router._extract_product_id(b''))

    def test_exact_four_bytes(self):
        data = struct.pack('<HH', 0x0000, 0x1234)
        self.assertEqual(self.router._extract_product_id(data), 0x1234)


class TestRegistrationMatching(unittest.TestCase):

    def setUp(self):
        bus = MagicMock()
        bus.add_signal_receiver = MagicMock()
        self.router = BleAdvertisementRouter(bus, version='1.0.0')

    def test_no_registrations_returns_false(self):
        self.assertFalse(self.router._should_process('AA:BB:CC:DD:EE:FF', 0x0131))
        self.assertFalse(self.router._has_registration('AA:BB:CC:DD:EE:FF', 0x0131, None))

    def test_mfg_registration_matches(self):
        self.router._mfg_registrations[0x0131] = {'/ble_advertisements/svc/mfgr/305'}
        self.assertTrue(self.router._should_process('AA:BB:CC:DD:EE:FF', 0x0131))
        self.assertTrue(self.router._has_registration('AA:BB:CC:DD:EE:FF', 0x0131, None))

    def test_mfg_registration_does_not_match_other_id(self):
        self.router._mfg_registrations[0x0131] = {'/ble_advertisements/svc/mfgr/305'}
        self.assertFalse(self.router._should_process('AA:BB:CC:DD:EE:FF', 0x9999))

    def test_mac_registration_matches(self):
        self.router._mac_registrations['AA:BB:CC:DD:EE:FF'] = {
            '/ble_advertisements/svc/addr/AABBCCDDEEFF'}
        self.assertTrue(self.router._should_process('AA:BB:CC:DD:EE:FF', 0x0000))
        self.assertTrue(self.router._has_registration('AA:BB:CC:DD:EE:FF', 0x0000, None))

    def test_pid_registration_matches(self):
        self.router._pid_registrations[(0x02E1, 0xA389)] = {
            '/ble_advertisements/svc/mfgr_product/737_41865'}
        self.assertTrue(self.router._should_process('AA:BB:CC:DD:EE:FF', 0x02E1))
        self.assertTrue(self.router._has_registration('AA:BB:CC:DD:EE:FF', 0x02E1, 0xA389))

    def test_pid_registration_does_not_match_wrong_product(self):
        self.router._pid_registrations[(0x02E1, 0xA389)] = {
            '/ble_advertisements/svc/mfgr_product/737_41865'}
        self.assertFalse(self.router._has_registration('AA:BB:CC:DD:EE:FF', 0x02E1, 0x0000))

    def test_pid_range_registration_matches(self):
        self.router._pid_range_registrations[(0x02E1, 100, 200)] = {
            '/ble_advertisements/svc/mfgr_product_range/737_100_200'}
        self.assertTrue(self.router._should_process('AA:BB:CC:DD:EE:FF', 0x02E1))
        self.assertTrue(self.router._has_registration('AA:BB:CC:DD:EE:FF', 0x02E1, 150))
        self.assertTrue(self.router._has_registration('AA:BB:CC:DD:EE:FF', 0x02E1, 100))
        self.assertTrue(self.router._has_registration('AA:BB:CC:DD:EE:FF', 0x02E1, 200))

    def test_pid_range_registration_excludes_outside(self):
        self.router._pid_range_registrations[(0x02E1, 100, 200)] = {
            '/ble_advertisements/svc/mfgr_product_range/737_100_200'}
        self.assertFalse(self.router._has_registration('AA:BB:CC:DD:EE:FF', 0x02E1, 99))
        self.assertFalse(self.router._has_registration('AA:BB:CC:DD:EE:FF', 0x02E1, 201))

    def test_mac_has_priority_over_mfg(self):
        """MAC match returns True even if mfg_id is unregistered."""
        self.router._mac_registrations['AA:BB:CC:DD:EE:FF'] = {
            '/ble_advertisements/svc/addr/AABBCCDDEEFF'}
        self.assertTrue(self.router._has_registration('AA:BB:CC:DD:EE:FF', 0x9999, None))


class TestGetRegisteredIds(unittest.TestCase):

    def setUp(self):
        bus = MagicMock()
        bus.add_signal_receiver = MagicMock()
        self.router = BleAdvertisementRouter(bus, version='1.0.0')

    def test_empty_returns_empty(self):
        self.assertEqual(self.router.get_registered_mfg_ids(), set())

    def test_mfg_ids_included(self):
        self.router._mfg_registrations[305] = set()
        self.router._mfg_registrations[737] = set()
        self.assertEqual(self.router.get_registered_mfg_ids(), {305, 737})

    def test_pid_mfg_ids_included(self):
        self.router._pid_registrations[(737, 100)] = set()
        self.assertIn(737, self.router.get_registered_mfg_ids())

    def test_pid_range_mfg_ids_included(self):
        self.router._pid_range_registrations[(737, 100, 200)] = set()
        self.assertIn(737, self.router.get_registered_mfg_ids())

    def test_deduplication(self):
        self.router._mfg_registrations[737] = set()
        self.router._pid_registrations[(737, 100)] = set()
        self.router._pid_range_registrations[(737, 50, 60)] = set()
        self.assertEqual(self.router.get_registered_mfg_ids(), {737})


class TestGetRegisteredMacs(unittest.TestCase):

    def setUp(self):
        bus = MagicMock()
        bus.add_signal_receiver = MagicMock()
        self.router = BleAdvertisementRouter(bus, version='1.0.0')

    def test_empty(self):
        self.assertEqual(self.router.get_registered_macs(), set())

    def test_returns_tap_format(self):
        self.router._mac_registrations['AA:BB:CC:DD:EE:FF'] = set()
        self.assertEqual(self.router.get_registered_macs(), {'aabbccddeeff'})


class TestRegistrationParsing(unittest.TestCase):
    """Test _parse_registrations with synthetic introspection XML."""

    def setUp(self):
        bus = MagicMock()
        bus.add_signal_receiver = MagicMock()
        self.router = BleAdvertisementRouter(bus, version='1.0.0')

    def _xml_with_nodes(self, *names):
        nodes = ''.join(f'<node name="{n}"/>' for n in names)
        return f'<node>{nodes}</node>'

    def test_mfgr_path(self):
        xml = self._xml_with_nodes()
        path = '/ble_advertisements/orion_tr/mfgr/737'
        self.router._parse_registrations('com.victronenergy.orion_tr', path, xml)
        self.assertIn(737, self.router._mfg_registrations)
        self.assertIn(path, self.router._mfg_registrations[737])

    def test_mfgr_product_path(self):
        xml = self._xml_with_nodes()
        path = '/ble_advertisements/svc/mfgr_product/737_41865'
        self.router._parse_registrations('com.victronenergy.svc', path, xml)
        self.assertIn((737, 41865), self.router._pid_registrations)

    def test_mfgr_product_range_path(self):
        xml = self._xml_with_nodes()
        path = '/ble_advertisements/svc/mfgr_product_range/737_100_200'
        self.router._parse_registrations('com.victronenergy.svc', path, xml)
        self.assertIn((737, 100, 200), self.router._pid_range_registrations)

    def test_addr_path_no_colons(self):
        xml = self._xml_with_nodes()
        path = '/ble_advertisements/svc/addr/AABBCCDDEEFF'
        self.router._parse_registrations('com.victronenergy.svc', path, xml)
        self.assertIn('AA:BB:CC:DD:EE:FF', self.router._mac_registrations)

    def test_addr_path_with_underscores(self):
        xml = self._xml_with_nodes()
        path = '/ble_advertisements/svc/addr/aa_bb_cc_dd_ee_ff'
        self.router._parse_registrations('com.victronenergy.svc', path, xml)
        self.assertIn('AA:BB:CC:DD:EE:FF', self.router._mac_registrations)

    def test_non_registration_path_ignored(self):
        xml = self._xml_with_nodes()
        path = '/some/other/path'
        self.router._parse_registrations('com.victronenergy.svc', path, xml)
        self.assertEqual(len(self.router._mfg_registrations), 0)
        self.assertEqual(len(self.router._mac_registrations), 0)


class TestServiceRemoval(unittest.TestCase):

    def setUp(self):
        bus = MagicMock()
        bus.add_signal_receiver = MagicMock()
        self.callback = MagicMock()
        self.router = BleAdvertisementRouter(bus, version='1.0.0', on_registrations_changed=self.callback)

    def test_remove_clears_mfg_registrations(self):
        path = '/ble_advertisements/orion_tr/mfgr/737'
        self.router._mfg_registrations[737] = {path}
        emitter = MagicMock()
        self.router._emitters[path] = emitter

        self.router._remove_service_registrations('orion_tr')

        self.assertNotIn(737, self.router._mfg_registrations)
        self.assertNotIn(path, self.router._emitters)
        emitter.remove_from_connection.assert_called_once()
        self.callback.assert_called()

    def test_remove_clears_mac_registrations(self):
        path = '/ble_advertisements/svc/addr/AABBCCDDEEFF'
        self.router._mac_registrations['AA:BB:CC:DD:EE:FF'] = {path}

        self.router._remove_service_registrations('svc')

        self.assertNotIn('AA:BB:CC:DD:EE:FF', self.router._mac_registrations)

    def test_remove_preserves_other_service(self):
        path_a = '/ble_advertisements/svc_a/mfgr/305'
        path_b = '/ble_advertisements/svc_b/mfgr/305'
        self.router._mfg_registrations[305] = {path_a, path_b}

        self.router._remove_service_registrations('svc_a')

        self.assertIn(305, self.router._mfg_registrations)
        self.assertEqual(self.router._mfg_registrations[305], {path_b})

    def test_no_callback_when_nothing_removed(self):
        self.router._remove_service_registrations('nonexistent')
        self.callback.assert_not_called()


class TestMultipleRegistrations(unittest.TestCase):
    """Multiple services can register for the same manufacturer ID."""

    def setUp(self):
        bus = MagicMock()
        bus.add_signal_receiver = MagicMock()
        self.router = BleAdvertisementRouter(bus, version='1.0.0')

    def test_two_services_same_mfg(self):
        path_a = '/ble_advertisements/svc_a/mfgr/305'
        path_b = '/ble_advertisements/svc_b/mfgr/305'
        self.router._mfg_registrations[305] = {path_a, path_b}

        emitter_a = MagicMock()
        emitter_b = MagicMock()
        self.router._emitters[path_a] = emitter_a
        self.router._emitters[path_b] = emitter_b

        self.router._emit_advertisement(
            'AA:BB:CC:DD:EE:FF', 305, b'\x00\x01\x02\x03', -60, 'hci0')

        emitter_a.Advertisement.assert_called_once()
        emitter_b.Advertisement.assert_called_once()


class TestProcessAdvertisement(unittest.TestCase):

    def setUp(self):
        bus = MagicMock()
        bus.add_signal_receiver = MagicMock()
        self.router = BleAdvertisementRouter(bus, version='1.0.0')

    def test_returns_false_when_no_registrations(self):
        result = self.router.process_advertisement(
            'aabbccddeeff', 0x0131, b'\x00\x01\x02\x03', -50, 'hci0')
        self.assertFalse(result)

    def test_returns_true_when_matched_and_emitted(self):
        path = '/ble_advertisements/svc/mfgr/305'
        self.router._mfg_registrations[305] = {path}
        emitter = MagicMock()
        self.router._emitters[path] = emitter

        result = self.router.process_advertisement(
            'aabbccddeeff', 305, b'\x00\x01\x02\x03', -50, 'hci0')
        self.assertTrue(result)
        emitter.Advertisement.assert_called_once()

    def test_mac_converted_to_colon_format(self):
        path = '/ble_advertisements/svc/addr/AABBCCDDEEFF'
        self.router._mac_registrations['AA:BB:CC:DD:EE:FF'] = {path}
        emitter = MagicMock()
        self.router._emitters[path] = emitter

        result = self.router.process_advertisement(
            'aabbccddeeff', 0x9999, b'\x00', -50, 'hci0')
        self.assertTrue(result)

        args = emitter.Advertisement.call_args[0]
        self.assertEqual(args[0], 'AA:BB:CC:DD:EE:FF')


class TestHasRegistrations(unittest.TestCase):

    def setUp(self):
        bus = MagicMock()
        bus.add_signal_receiver = MagicMock()
        self.router = BleAdvertisementRouter(bus, version='1.0.0')

    def test_empty_is_false(self):
        self.assertFalse(self.router.has_registrations())

    def test_mfg_is_true(self):
        self.router._mfg_registrations[305] = set()
        self.assertTrue(self.router.has_registrations())

    def test_mac_is_true(self):
        self.router._mac_registrations['AA:BB:CC:DD:EE:FF'] = set()
        self.assertTrue(self.router.has_registrations())


class TestRootObject(unittest.TestCase):
    """The /ble_advertisements root object exposes GetVersion / GetStatus /
    GetHeartbeat for client-side service-presence checks (compat with the
    standalone dbus-ble-advertisements project)."""

    def setUp(self):
        bus = MagicMock()
        bus.add_signal_receiver = MagicMock()
        self.router = BleAdvertisementRouter(bus, version='1.2.3')

    def test_get_version_returns_passed_value(self):
        self.assertEqual(self.router._root.GetVersion(), '1.2.3')

    def test_get_status_running_when_fresh(self):
        self.assertEqual(self.router._root.GetStatus(), 'running')

    def test_get_status_stale_when_heartbeat_old(self):
        self.router._root._heartbeat -= HEARTBEAT_STALE_AFTER_SECONDS + 1
        self.assertEqual(self.router._root.GetStatus(), 'stale')

    def test_get_heartbeat_returns_timestamp(self):
        ts = self.router._root.GetHeartbeat()
        self.assertIsInstance(ts, float)
        self.assertGreater(ts, 0)

    def test_process_advertisement_bumps_heartbeat(self):
        original = self.router._root._heartbeat
        self.router._root._heartbeat -= 100  # pretend it's stale-ish
        self.router.process_advertisement(
            'aabbccddeeff', 0x02E1, b'\x10\x02\x00\x00', -50, 'hci0')
        self.assertGreaterEqual(self.router._root._heartbeat, original - 1)


if __name__ == '__main__':
    unittest.main()
