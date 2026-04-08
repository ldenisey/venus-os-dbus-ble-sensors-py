"""Tests for passive BLE scanning with threaded AdvertisementMonitor1."""
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
import dbus_ble_sensors as _dbus_ble_sensors_mod
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


class TestRunScanners(unittest.TestCase):
    """Verify _run_scanners: passive-first with per-adapter backoff and active fallback."""

    def _run(self, coro, timeout=10):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(asyncio.wait_for(coro, timeout=timeout))
        except asyncio.TimeoutError:
            pass
        finally:
            loop.close()

    def _make_obj(self):
        """Create a DbusBleSensors with __init__ bypassed."""
        obj = object.__new__(DbusBleSensors)
        obj._ignored_mac = {}
        obj._known_mac = {}
        obj._adapters = ['hci0']
        obj._scan_buffer = []
        obj._scan_buffer_lock = __import__('threading').Lock()
        obj._scanner_stop = __import__('threading').Event()
        return obj

    @patch('dbus_ble_sensors.bleak.BleakScanner')
    def test_passive_scanner_uses_correct_params(self, mock_scanner_cls):
        """BleakScanner should get passive mode + or_patterns."""
        obj = self._make_obj()
        scanner_instance = AsyncMock()
        scanner_instance.start = AsyncMock(side_effect=lambda: obj._scanner_stop.set())
        mock_scanner_cls.return_value = scanner_instance

        self._run(obj._run_scanners())

        first_call = mock_scanner_cls.call_args_list[0]
        kwargs = first_call.kwargs
        self.assertEqual(kwargs.get('scanning_mode'), 'passive')
        self.assertEqual(kwargs['bluez']['adapter'], 'hci0')
        self.assertEqual(kwargs['bluez']['or_patterns'], PASSIVE_SCAN_OR_PATTERNS)

    @patch('dbus_ble_sensors.bleak.BleakScanner')
    def test_retries_with_backoff_on_failure(self, mock_scanner_cls):
        """If scanner.start() fails, should retry with exponential backoff."""
        attempt_count = [0]

        def make_scanner(*args, **kwargs):
            attempt_count[0] += 1
            scanner = AsyncMock()
            if attempt_count[0] == 1:
                scanner.start = AsyncMock(side_effect=Exception("EBUSY"))
            else:
                scanner.start = AsyncMock()
            return scanner

        mock_scanner_cls.side_effect = make_scanner

        obj = self._make_obj()

        async def run():
            async def stopper():
                while attempt_count[0] < 2:
                    await asyncio.sleep(0.1)
                obj._scanner_stop.set()
            await asyncio.gather(obj._run_scanners(), stopper())

        self._run(run())

        self.assertGreaterEqual(attempt_count[0], 2,
            "Should have retried after first failure")

    @patch('dbus_ble_sensors.DbusBleSensors._power_cycle_adapter', new_callable=AsyncMock)
    @patch('dbus_ble_sensors.bleak.BleakScanner')
    def test_power_cycles_before_active_fallback(self, mock_scanner_cls, mock_power_cycle):
        """After MAX_PASSIVE_RETRIES, should power-cycle and retry passive before active."""
        attempt_count = [0]
        modes_seen = []

        def make_scanner(*args, **kwargs):
            attempt_count[0] += 1
            mode = kwargs.get('scanning_mode')
            modes_seen.append(mode)
            scanner = AsyncMock()
            if mode == 'passive':
                scanner.start = AsyncMock(side_effect=Exception("EBUSY"))
            else:
                def stop_after_active():
                    obj._scanner_stop.set()
                scanner.start = AsyncMock()
                scanner.stop = AsyncMock(side_effect=stop_after_active)
            return scanner

        mock_scanner_cls.side_effect = make_scanner

        obj = self._make_obj()

        with patch.object(_dbus_ble_sensors_mod, 'SCANNER_INITIAL_BACKOFF', 0), \
             patch.object(_dbus_ble_sensors_mod, 'SCANNER_MAX_BACKOFF', 0), \
             patch.object(_dbus_ble_sensors_mod, 'SCANNER_MAX_PASSIVE_RETRIES', 2), \
             patch.object(_dbus_ble_sensors_mod, 'SCANNER_ACTIVE_CYCLE_DURATION', 0):
            self._run(obj._run_scanners(), timeout=10)

        mock_power_cycle.assert_called_once_with('hci0')
        passive_count = sum(1 for m in modes_seen if m == 'passive')
        active_count = sum(1 for m in modes_seen if m is None)
        self.assertGreaterEqual(passive_count, 4,
            "Should try passive 2x, power-cycle, then passive 2x more before active")
        self.assertGreaterEqual(active_count, 1,
            "Should fall back to active only after power-cycle + passive retries fail")

    @patch('dbus_ble_sensors.DbusBleSensors._power_cycle_adapter', new_callable=AsyncMock)
    @patch('dbus_ble_sensors.bleak.BleakScanner')
    def test_power_cycle_recovers_passive(self, mock_scanner_cls, mock_power_cycle):
        """If power-cycle clears the HCI state, passive should succeed without active."""
        attempt_count = [0]
        modes_seen = []

        def make_scanner(*args, **kwargs):
            attempt_count[0] += 1
            mode = kwargs.get('scanning_mode')
            modes_seen.append(mode)
            scanner = AsyncMock()
            # Fail passive 2 times (pre-power-cycle), then succeed
            if mode == 'passive' and attempt_count[0] <= 2:
                scanner.start = AsyncMock(side_effect=Exception("EBUSY"))
            else:
                scanner.start = AsyncMock(side_effect=lambda: obj._scanner_stop.set())
            return scanner

        mock_scanner_cls.side_effect = make_scanner

        obj = self._make_obj()

        with patch.object(_dbus_ble_sensors_mod, 'SCANNER_INITIAL_BACKOFF', 0), \
             patch.object(_dbus_ble_sensors_mod, 'SCANNER_MAX_BACKOFF', 0), \
             patch.object(_dbus_ble_sensors_mod, 'SCANNER_MAX_PASSIVE_RETRIES', 2):
            self._run(obj._run_scanners(), timeout=10)

        mock_power_cycle.assert_called_once_with('hci0')
        active_count = sum(1 for m in modes_seen if m is None)
        self.assertEqual(active_count, 0,
            "Should never reach active mode when power-cycle recovers passive")

    @patch('dbus_ble_sensors.bleak.BleakScanner')
    def test_multiple_adapters(self, mock_scanner_cls):
        """Should start a scanner per adapter."""
        obj = self._make_obj()
        obj._adapters = ['hci0', 'hci1']
        started = [0]

        def make_scanner(*args, **kwargs):
            s = AsyncMock()
            def on_start():
                started[0] += 1
                if started[0] >= len(obj._adapters):
                    obj._scanner_stop.set()
            s.start = AsyncMock(side_effect=on_start)
            return s

        mock_scanner_cls.side_effect = make_scanner

        self._run(obj._run_scanners())

        adapters_used = [c.kwargs['bluez']['adapter'] for c in mock_scanner_cls.call_args_list]
        self.assertIn('hci0', adapters_used)
        self.assertIn('hci1', adapters_used)

    @patch('dbus_ble_sensors.bleak.BleakScanner')
    def test_collects_advertisements_to_buffer(self, mock_scanner_cls):
        """detection_callback should append to _scan_buffer."""
        obj = self._make_obj()
        fake_device = MagicMock()
        fake_ad = MagicMock()

        scanner_instance = AsyncMock()

        def on_start():
            cb = mock_scanner_cls.call_args.kwargs.get('detection_callback')
            if cb:
                cb(fake_device, fake_ad)
            obj._scanner_stop.set()

        scanner_instance.start = AsyncMock(side_effect=on_start)
        mock_scanner_cls.return_value = scanner_instance

        self._run(obj._run_scanners())

        self.assertEqual(len(obj._scan_buffer), 1)
        self.assertIs(obj._scan_buffer[0][0], fake_device)
        self.assertIs(obj._scan_buffer[0][1], fake_ad)

    @patch('dbus_ble_sensors.bleak.BleakScanner')
    def test_starts_passive_not_active(self, mock_scanner_cls):
        """Initial scanner should always be passive mode."""
        obj = self._make_obj()
        scanner_instance = AsyncMock()
        scanner_instance.start = AsyncMock(side_effect=lambda: obj._scanner_stop.set())
        mock_scanner_cls.return_value = scanner_instance

        self._run(obj._run_scanners())

        first_call = mock_scanner_cls.call_args_list[0]
        self.assertEqual(first_call.kwargs.get('scanning_mode'), 'passive')


class TestProcessAdvertisement(unittest.TestCase):
    """Verify _process_advertisement handles device/advertisement data."""

    def _make_obj(self):
        obj = object.__new__(DbusBleSensors)
        obj._ignored_mac = {}
        obj._known_mac = {}
        return obj

    def test_ignores_device_without_manufacturer_data(self):
        fake_device = MagicMock()
        fake_device.address = 'AA:BB:CC:DD:EE:FF'
        fake_device.name = 'TestDevice'
        fake_ad = MagicMock()
        fake_ad.manufacturer_data = None

        obj = self._make_obj()
        obj._process_advertisement(fake_device, fake_ad)

        self.assertIn('aabbccddeeff', obj._ignored_mac)


if __name__ == '__main__':
    unittest.main()
