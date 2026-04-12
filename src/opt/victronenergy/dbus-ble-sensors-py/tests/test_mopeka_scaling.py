"""
Tests for Mopeka sensor data parsing and scaling calculations.

Uses raw BLE advertisement captures from real Mopeka Pro LPG sensors on
a 2025 Airstream Flying Cloud 30' FB Bunk (two 30lb steel propane tanks).

Verifies:
  - Register parsing matches the C implementation (mopeka.c)
  - Temperature-dependent polynomial scaling of RawValue
  - Butane ratio scaling matches C: both coefficients multiplied by r
  - End-to-end pipeline: raw bytes → scaled cm → level percentage
"""
import sys
import os
import unittest
from unittest.mock import MagicMock

# Mock D-Bus modules before any imports that need them
for _mod in ['dbus', 'dbus.mainloop.glib', 'vedbus', 'settingsdevice', 'dbusmonitor']:
    sys.modules[_mod] = MagicMock()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'ext'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'ext', 'velib_python'))

from ble_device_mopeka import BleDeviceMopeka
from ble_role_tank import BleRoleTank
from ble_role import BleRole


# Raw advertisement captures from btmon (manufacturer_id=0x0059 payload only)
# Sensor: Mopeka Pro LPG, HW ID 3
RAW_STEEL_R = [
    # CB:15:7D:29:61:44 — NIC bytes at [5:8] = 29 61 44
    bytes.fromhex('03593adb80296144e6f9'),
    bytes.fromhex('03593adb80296144edfd'),
    bytes.fromhex('03593adb80296144e900'),
    bytes.fromhex('035a3adb80296144ebf9'),
    bytes.fromhex('03593adb80296144ebfb'),
    bytes.fromhex('03593adb80296144f0f6'),
]

RAW_STEEL_L = [
    # F4:5F:E6:A3:DA:F4 — NIC bytes at [5:8] = a3 da f4
    bytes.fromhex('03593bdc80a3daf40419'),
    bytes.fromhex('03593bdc80a3daf40416'),
    bytes.fromhex('035a3bdc80a3daf40019'),
    bytes.fromhex('035a3bdc80a3daf4fd12'),
    bytes.fromhex('03593bdc80a3daf4001b'),
    bytes.fromhex('03593bdc80a3daf40619'),
    bytes.fromhex('03593bdd80a3daf40419'),
]


class TestMopekaRegisterParsing(unittest.TestCase):
    """Test that register parsing extracts correct values from raw bytes."""

    @classmethod
    def setUpClass(cls):
        BleRole.load_classes(os.path.dirname(os.path.abspath(__file__)))

    def _make_device(self, mac, raw):
        dev = BleDeviceMopeka(mac)
        dev.configure(raw)
        dev._load_configuration()
        return dev

    def test_steel_r_parsing(self):
        raw = RAW_STEEL_R[0]  # 03593adb80296144e6f9
        dev = self._make_device('cb157d296144', raw)
        parsed = dev._parse_manufacturer_data(raw)

        self.assertEqual(parsed['tank']['HardwareID'], 3)
        self.assertEqual(parsed['tank']['TankLevelExtension'], 0)
        self.assertEqual(parsed['tank']['BatteryVoltage'], 89 / 32)  # 2.78125
        self.assertEqual(parsed['tank']['Temperature'], 18.0)  # 58 - 40
        self.assertEqual(parsed['tank']['RawValue'], 219)

        self.assertNotIn('temperature', parsed,
                         "Mopeka should not have a separate temperature role")

    def test_single_tank_role_only(self):
        """Mopeka creates one tank service, not separate temperature/movement services.
        Matches C implementation which exposes /Temperature on the tank service."""
        raw = RAW_STEEL_R[0]
        dev = self._make_device('cb157d296144', raw)
        parsed = dev._parse_manufacturer_data(raw)
        self.assertEqual(list(parsed.keys()), ['tank'])

    def test_steel_l_parsing(self):
        raw = RAW_STEEL_L[0]  # 03593bdc80a3daf40419
        dev = self._make_device('f45fe6a3daf4', raw)
        parsed = dev._parse_manufacturer_data(raw)

        self.assertEqual(parsed['tank']['HardwareID'], 3)
        self.assertEqual(parsed['tank']['Temperature'], 19.0)  # 59 - 40
        self.assertEqual(parsed['tank']['RawValue'], 220)

    def test_steel_l_raw_221(self):
        raw = RAW_STEEL_L[6]  # 03593bdd80... — RawValue should be 221
        dev = self._make_device('f45fe6a3daf4', raw)
        parsed = dev._parse_manufacturer_data(raw)
        self.assertEqual(parsed['tank']['RawValue'], 221)

    def test_temperature_in_tank_role(self):
        """Temperature must be in the tank role data for scaling calculation.
        The C implementation (mopeka_xlate_level) reads Temperature from the
        flat VeItem tree; the Python port must include it in the tank role."""
        raw = RAW_STEEL_R[0]
        dev = self._make_device('cb157d296144', raw)
        parsed = dev._parse_manufacturer_data(raw)
        self.assertIn('Temperature', parsed['tank'],
                      "Temperature must be available in tank role for scaling")

    def test_nic_check_rejects_wrong_mac(self):
        raw = RAW_STEEL_R[0]  # NIC = 29 61 44
        dev = self._make_device('aabbccddeeff', raw)
        dev.configure(raw)
        dev._load_configuration()
        self.assertFalse(dev.check_manufacturer_data(raw))

    def test_nic_check_accepts_correct_mac(self):
        raw = RAW_STEEL_R[0]
        dev = self._make_device('cb157d296144', raw)
        self.assertTrue(dev.check_manufacturer_data(raw))

    def test_all_steel_r_samples_pass_nic(self):
        for i, raw in enumerate(RAW_STEEL_R):
            dev = self._make_device('cb157d296144', raw)
            self.assertTrue(dev.check_manufacturer_data(raw),
                            f"Steel R sample {i} failed NIC check")

    def test_all_steel_l_samples_pass_nic(self):
        for i, raw in enumerate(RAW_STEEL_L):
            dev = self._make_device('f45fe6a3daf4', raw)
            self.assertTrue(dev.check_manufacturer_data(raw),
                            f"Steel L sample {i} failed NIC check")

    def test_cross_contamination_rejected(self):
        """Steel R data must NOT pass Steel L's NIC check, and vice versa."""
        raw_r = RAW_STEEL_R[0]
        raw_l = RAW_STEEL_L[0]

        dev_r = self._make_device('cb157d296144', raw_r)
        dev_l = self._make_device('f45fe6a3daf4', raw_l)

        self.assertFalse(dev_r.check_manufacturer_data(raw_l),
                         "Steel R device must reject Steel L data")
        self.assertFalse(dev_l.check_manufacturer_data(raw_r),
                         "Steel L device must reject Steel R data")

    def test_battery_voltage_variation(self):
        """Battery voltage should vary between samples (byte 1 = 0x59 or 0x5a)."""
        dev = self._make_device('f45fe6a3daf4', RAW_STEEL_L[0])
        voltages = set()
        for raw in RAW_STEEL_L:
            parsed = dev._parse_manufacturer_data(raw)
            voltages.add(parsed['tank']['BatteryVoltage'])
        self.assertTrue(len(voltages) >= 2,
                        f"Expected battery voltage variation, got {voltages}")


class TestMopekaScaling(unittest.TestCase):
    """Test the temperature-dependent polynomial scaling of RawValue."""

    @classmethod
    def setUpClass(cls):
        BleRole.load_classes(os.path.dirname(os.path.abspath(__file__)))

    def _make_device(self, mac, raw):
        dev = BleDeviceMopeka(mac)
        dev.configure(raw)
        dev._load_configuration()
        return dev

    def _mock_role_service(self, butane_ratio=0, fluid_type=8):
        svc = {
            'ButaneRatio': butane_ratio,
            'FluidType': fluid_type,
            'BatteryVoltage': 2.78125,
        }
        mock = MagicMock()
        mock.__getitem__ = lambda self_m, key: svc[key]
        mock.ble_role = MagicMock()
        mock.ble_role.NAME = 'tank'
        return mock

    def test_butane_scale_zero_ratio(self):
        """With ButaneRatio=0, butane contribution must be exactly 0.
        Matches C: mopeka_coefs_butane[0] * r + mopeka_coefs_butane[1] * r * temp"""
        dev = BleDeviceMopeka('000000000000')
        result = dev._get_scale_butane(0, 58)
        self.assertEqual(result, 0.0)

    def test_butane_scale_nonzero_ratio(self):
        """With ButaneRatio=50, verify against C formula."""
        dev = BleDeviceMopeka('000000000000')
        r = 50 / 100.0
        temp = 58
        expected = 0.03615 * r + 0.000815 * r * temp
        result = dev._get_scale_butane(50, temp)
        self.assertAlmostEqual(result, expected, places=10)

    def test_butane_scale_full_ratio(self):
        dev = BleDeviceMopeka('000000000000')
        r = 1.0
        temp = 58
        expected = 0.03615 * r + 0.000815 * r * temp
        result = dev._get_scale_butane(100, temp)
        self.assertAlmostEqual(result, expected, places=10)

    def test_scaling_steel_r(self):
        """Verify scaling matches C mopeka_xlate_level for Steel R raw capture."""
        raw = RAW_STEEL_R[0]
        dev = self._make_device('cb157d296144', raw)
        parsed = dev._parse_manufacturer_data(raw)
        tank_data = dict(parsed['tank'])

        role_svc = self._mock_role_service(butane_ratio=0)
        dev.update_data(role_svc, tank_data)

        # C calculation: temp=58, raw=219, coefs_lpg, butane_scale=0
        # scale = 0.573045 + (-0.002822 * 58) + (-0.00000535 * 58 * 58) = 0.391372
        # RawValue = (219 * 0.391372) / 10 = 8.5710
        self.assertAlmostEqual(tank_data['RawValue'], 8.571, places=2)

    def test_scaling_steel_l(self):
        """Verify scaling for Steel L (different temperature and raw value)."""
        raw = RAW_STEEL_L[0]  # temp=19°C, raw=220
        dev = self._make_device('f45fe6a3daf4', raw)
        parsed = dev._parse_manufacturer_data(raw)
        tank_data = dict(parsed['tank'])

        role_svc = self._mock_role_service(butane_ratio=0)
        dev.update_data(role_svc, tank_data)

        # temp=59, raw=220, coefs_lpg, butane_scale=0
        # scale = 0.573045 + (-0.002822 * 59) + (-0.00000535 * 59 * 59) = 0.387924
        # RawValue = (220 * 0.387924) / 10 = 8.5343
        self.assertAlmostEqual(tank_data['RawValue'], 8.534, places=2)

    def test_scaling_skipped_for_non_tank_role(self):
        """update_data must be a no-op when called with a non-tank role."""
        raw = RAW_STEEL_R[0]
        dev = self._make_device('cb157d296144', raw)

        fake_data = {'Temperature': 18.0, 'BatteryVoltage': 2.78125}
        original = fake_data.copy()

        temp_svc = MagicMock()
        temp_svc.ble_role = MagicMock()
        temp_svc.ble_role.NAME = 'temperature'
        dev.update_data(temp_svc, fake_data)
        self.assertEqual(fake_data, original, "Non-tank role data must not be modified")

    def test_sensors_produce_different_values(self):
        """Steel R and Steel L must produce measurably different scaled values."""
        dev_r = self._make_device('cb157d296144', RAW_STEEL_R[0])
        dev_l = self._make_device('f45fe6a3daf4', RAW_STEEL_L[0])

        parsed_r = dev_r._parse_manufacturer_data(RAW_STEEL_R[0])
        parsed_l = dev_l._parse_manufacturer_data(RAW_STEEL_L[0])
        tank_r = dict(parsed_r['tank'])
        tank_l = dict(parsed_l['tank'])

        role_svc = self._mock_role_service(butane_ratio=0)
        dev_r.update_data(role_svc, tank_r)
        dev_l.update_data(role_svc, tank_l)

        self.assertNotEqual(tank_r['RawValue'], tank_l['RawValue'],
                            "Steel R and L must have different scaled values")
        self.assertNotEqual(parsed_r['tank']['Temperature'],
                            parsed_l['tank']['Temperature'],
                            "Steel R and L must have different temperatures")


class TestMopekaEndToEnd(unittest.TestCase):
    """Test the full pipeline: raw bytes → scaling → level calculation."""

    @classmethod
    def setUpClass(cls):
        BleRole.load_classes(os.path.dirname(os.path.abspath(__file__)))

    def _make_device(self, mac, raw):
        dev = BleDeviceMopeka(mac)
        dev.configure(raw)
        dev._load_configuration()
        return dev

    def _mock_role_service(self, raw_empty=0.0, raw_full=40.4, capacity=0.02649788):
        return {
            'ButaneRatio': 0,
            'FluidType': 8,
            'BatteryVoltage': 2.78125,
            'RawValue': 0.0,
            'RawValueEmpty': raw_empty,
            'RawValueFull': raw_full,
            'Capacity': capacity,
            'Shape': '',
            'Level': 0,
            'Remaining': 0.0,
        }

    def test_steel_r_full_pipeline(self):
        """Raw bytes → Mopeka scaling → tank level computation → ~21%."""
        raw = RAW_STEEL_R[0]
        dev = self._make_device('cb157d296144', raw)
        parsed = dev._parse_manufacturer_data(raw)
        tank_data = dict(parsed['tank'])

        role_svc_mock = MagicMock()
        role_svc_mock.__getitem__ = lambda s, k: {'ButaneRatio': 0, 'FluidType': 8}[k]
        role_svc_mock.ble_role = MagicMock()
        role_svc_mock.ble_role.NAME = 'tank'

        # Step 1: Device scaling (must run first, per C xlate pattern)
        dev.update_data(role_svc_mock, tank_data)
        scaled_raw = tank_data['RawValue']
        self.assertAlmostEqual(scaled_raw, 8.571, places=2)

        # Step 2: Role level computation
        tank_role = BleRoleTank(config={'flags': []})
        tank_role._parse_shape_str('')
        dbus_svc = self._mock_role_service()
        level, remaining, status = tank_role._compute_level(
            rawValue=scaled_raw,
            empty=dbus_svc['RawValueEmpty'],
            full=dbus_svc['RawValueFull'],
            capacity=dbus_svc['Capacity'],
        )
        self.assertEqual(level, 21)
        self.assertEqual(status, 0)

    def test_steel_l_full_pipeline(self):
        """Steel L should also produce ~21% but with different intermediate values."""
        raw = RAW_STEEL_L[0]
        dev = self._make_device('f45fe6a3daf4', raw)
        parsed = dev._parse_manufacturer_data(raw)
        tank_data = dict(parsed['tank'])

        role_svc_mock = MagicMock()
        role_svc_mock.__getitem__ = lambda s, k: {'ButaneRatio': 0, 'FluidType': 8}[k]
        role_svc_mock.ble_role = MagicMock()
        role_svc_mock.ble_role.NAME = 'tank'

        dev.update_data(role_svc_mock, tank_data)
        scaled_raw = tank_data['RawValue']
        self.assertAlmostEqual(scaled_raw, 8.534, places=2)

        tank_role = BleRoleTank(config={'flags': []})
        tank_role._parse_shape_str('')
        dbus_svc = self._mock_role_service()
        level, remaining, status = tank_role._compute_level(
            rawValue=scaled_raw,
            empty=dbus_svc['RawValueEmpty'],
            full=dbus_svc['RawValueFull'],
            capacity=dbus_svc['Capacity'],
        )
        self.assertEqual(level, 21)
        self.assertEqual(status, 0)

    def test_wrong_execution_order_gives_wrong_level(self):
        """If role update_data runs before device update_data, Level is wrong.
        This regression test ensures the execution order fix stays in place."""
        raw = RAW_STEEL_R[0]
        dev = self._make_device('cb157d296144', raw)
        parsed = dev._parse_manufacturer_data(raw)
        tank_data = dict(parsed['tank'])

        # Wrong order: role first (computes Level from UNSCALED RawValue=219)
        tank_role = BleRoleTank(config={'flags': []})
        tank_role._parse_shape_str('')
        dbus_svc = self._mock_role_service()
        level_wrong, _, _ = tank_role._compute_level(
            rawValue=float(tank_data['RawValue']),  # 219 (unscaled!)
            empty=dbus_svc['RawValueEmpty'],
            full=dbus_svc['RawValueFull'],
            capacity=dbus_svc['Capacity'],
        )
        self.assertEqual(level_wrong, 100,
                         "Unscaled RawValue should overflow to 100%")

        # Correct order: device first (scales RawValue), then role
        role_svc_mock = MagicMock()
        role_svc_mock.__getitem__ = lambda s, k: {'ButaneRatio': 0, 'FluidType': 8}[k]
        role_svc_mock.ble_role = MagicMock()
        role_svc_mock.ble_role.NAME = 'tank'
        dev.update_data(role_svc_mock, tank_data)

        level_correct, _, _ = tank_role._compute_level(
            rawValue=float(tank_data['RawValue']),  # ~8.57 (scaled)
            empty=dbus_svc['RawValueEmpty'],
            full=dbus_svc['RawValueFull'],
            capacity=dbus_svc['Capacity'],
        )
        self.assertEqual(level_correct, 21,
                         "Scaled RawValue should give ~21%")


if __name__ == '__main__':
    unittest.main()
