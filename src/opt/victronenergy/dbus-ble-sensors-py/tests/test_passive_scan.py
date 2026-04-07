"""Tests for passive BLE scanning with AdvertisementMonitor1 fallback."""
import sys
import os
import types
from unittest.mock import MagicMock

# ── Mock Venus OS modules unavailable outside the target device ──────
# These must be injected before importing dbus_ble_sensors because its
# import chain (ble_device -> dbus_ble_service -> vedbus, etc.) would
# fail on any non-Venus-OS machine.

_MOCK_MODULES = [
    'dbus', 'dbus.mainloop', 'dbus.mainloop.glib', 'dbus.service',
    'gi', 'gi.repository', 'gi.repository.GLib',
    'gbulb',
    'vedbus', 'logger', 've_utils',
    'dbus_settings_service', 'dbus_ble_service', 'dbus_role_service',
    'ble_device', 'ble_role', 've_types', 'man_id', 'conf',
]

for mod_name in _MOCK_MODULES:
    sys.modules.setdefault(mod_name, MagicMock())

# Provide specific attributes the import chain expects
sys.modules['dbus'].SystemBus = MagicMock
sys.modules['dbus'].SessionBus = MagicMock
sys.modules['dbus.mainloop.glib'].DBusGMainLoop = MagicMock()
sys.modules['gbulb'].install = MagicMock()
sys.modules['gbulb'].GLibEventLoopPolicy = type('GLibEventLoopPolicy', (), {})
sys.modules['logger'].setup_logging = MagicMock()
sys.modules['conf'].SCAN_TIMEOUT = 15
sys.modules['conf'].SCAN_SLEEP = 5
sys.modules['conf'].IGNORED_DEVICES_TIMEOUT = 600
sys.modules['conf'].DEVICE_SERVICES_TIMEOUT = 1800
sys.modules['conf'].PROCESS_VERSION = '1.1.0'
sys.modules['man_id'].MAN_NAMES = {}
sys.modules['ble_device'].BleDevice = type('BleDevice', (), {'DEVICE_CLASSES': {}, 'load_classes': classmethod(lambda cls, p: None)})
sys.modules['ble_role'].BleRole = type('BleRole', (), {'load_classes': classmethod(lambda cls, p: None)})
sys.modules['dbus_ble_service'].DbusBleService = MagicMock

# ── Now safe to import ───────────────────────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'ext'))

import unittest
from unittest.mock import patch, AsyncMock
import asyncio

from dbus_ble_sensors import DbusBleSensors, PASSIVE_SCAN_OR_PATTERNS
from bleak.assigned_numbers import AdvertisementDataType


class TestPassiveScanOrPatterns(unittest.TestCase):
    """Verify the OR patterns constant is well-formed."""

    def test_patterns_not_empty(self):
        self.assertGreater(len(PASSIVE_SCAN_OR_PATTERNS), 0)

    def test_patterns_are_tuples(self):
        for pat in PASSIVE_SCAN_OR_PATTERNS:
            self.assertIsInstance(pat, tuple)
            self.assertEqual(len(pat), 3, f"Pattern {pat} should be (offset, ad_type, value)")

    def test_patterns_use_flags_ad_type(self):
        for offset, ad_type, value in PASSIVE_SCAN_OR_PATTERNS:
            self.assertEqual(offset, 0)
            self.assertEqual(ad_type, AdvertisementDataType.FLAGS)
            self.assertIsInstance(value, bytes)
            self.assertEqual(len(value), 1)

    def test_common_flag_values_covered(self):
        flag_bytes = {pat[2][0] for pat in PASSIVE_SCAN_OR_PATTERNS}
        self.assertIn(0x06, flag_bytes, "LE General Discoverable + BR/EDR Not Supported")
        self.assertIn(0x02, flag_bytes, "LE General Discoverable")
        self.assertIn(0x1a, flag_bytes, "LE General + BR/EDR Not Supported + Dual-Mode")


class TestScanMethodPassiveMode(unittest.TestCase):
    """Verify _scan() uses passive mode with fallback to active."""

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def _make_obj(self):
        """Create a DbusBleSensors with __init__ bypassed."""
        obj = object.__new__(DbusBleSensors)
        obj._ignored_mac = {}
        obj._known_mac = {}
        return obj

    @patch('dbus_ble_sensors.asyncio.sleep', new_callable=AsyncMock)
    @patch('dbus_ble_sensors.bleak.BleakScanner')
    def test_scan_uses_passive_mode(self, mock_scanner_cls, mock_sleep):
        """BleakScanner should be created with scanning_mode='passive'."""
        scanner_instance = AsyncMock()
        mock_scanner_cls.return_value = scanner_instance
        scanner_instance.__aenter__ = AsyncMock(return_value=scanner_instance)
        scanner_instance.__aexit__ = AsyncMock(return_value=False)

        self._run(self._make_obj()._scan('hci0'))

        mock_scanner_cls.assert_called_once()
        kwargs = mock_scanner_cls.call_args.kwargs
        self.assertEqual(kwargs.get('scanning_mode'), 'passive')
        self.assertEqual(kwargs['bluez']['adapter'], 'hci0')
        self.assertEqual(kwargs['bluez']['or_patterns'], PASSIVE_SCAN_OR_PATTERNS)

    @patch('dbus_ble_sensors.asyncio.sleep', new_callable=AsyncMock)
    @patch('dbus_ble_sensors.bleak.BleakScanner')
    def test_scan_falls_back_to_active_on_passive_failure(self, mock_scanner_cls, mock_sleep):
        """If passive scan raises, should fall back to active scan."""
        passive_scanner = AsyncMock()
        passive_scanner.__aenter__ = AsyncMock(
            side_effect=Exception("passive scanning mode requires bluez or_patterns")
        )
        passive_scanner.__aexit__ = AsyncMock(return_value=False)

        active_scanner = AsyncMock()
        active_scanner.__aenter__ = AsyncMock(return_value=active_scanner)
        active_scanner.__aexit__ = AsyncMock(return_value=False)

        mock_scanner_cls.side_effect = [passive_scanner, active_scanner]

        self._run(self._make_obj()._scan('hci0'))

        self.assertEqual(mock_scanner_cls.call_count, 2,
                         "Should create two scanners: passive attempt then active fallback")

        first_kwargs = mock_scanner_cls.call_args_list[0].kwargs
        self.assertEqual(first_kwargs.get('scanning_mode'), 'passive')

        second_kwargs = mock_scanner_cls.call_args_list[1].kwargs
        self.assertNotIn('scanning_mode', second_kwargs,
                         "Active fallback should not set scanning_mode")
        self.assertEqual(second_kwargs['bluez']['adapter'], 'hci0')

    @patch('dbus_ble_sensors.asyncio.sleep', new_callable=AsyncMock)
    @patch('dbus_ble_sensors.bleak.BleakScanner')
    def test_scan_passes_detection_callback(self, mock_scanner_cls, mock_sleep):
        """BleakScanner should receive a callable detection_callback."""
        scanner_instance = AsyncMock()
        mock_scanner_cls.return_value = scanner_instance
        scanner_instance.__aenter__ = AsyncMock(return_value=scanner_instance)
        scanner_instance.__aexit__ = AsyncMock(return_value=False)

        self._run(self._make_obj()._scan('hci0'))

        callback = mock_scanner_cls.call_args.kwargs.get('detection_callback')
        self.assertIsNotNone(callback)
        self.assertTrue(callable(callback))

    @patch('dbus_ble_sensors.asyncio.sleep', new_callable=AsyncMock)
    @patch('dbus_ble_sensors.bleak.BleakScanner')
    def test_scan_adapter_passed_in_bluez_dict(self, mock_scanner_cls, mock_sleep):
        """Adapter name should be in bluez dict, not as a top-level kwarg."""
        scanner_instance = AsyncMock()
        mock_scanner_cls.return_value = scanner_instance
        scanner_instance.__aenter__ = AsyncMock(return_value=scanner_instance)
        scanner_instance.__aexit__ = AsyncMock(return_value=False)

        self._run(self._make_obj()._scan('hci1'))

        kwargs = mock_scanner_cls.call_args.kwargs
        self.assertNotIn('adapter', kwargs,
                         "adapter should be inside bluez dict, not top-level")
        self.assertEqual(kwargs['bluez']['adapter'], 'hci1')


if __name__ == '__main__':
    unittest.main()
