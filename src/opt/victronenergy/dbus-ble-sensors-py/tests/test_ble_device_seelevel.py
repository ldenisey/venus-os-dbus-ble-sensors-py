"""
Tests for SeeLevel BTP3 and BTP7 BLE advertisement parsing.

All test payloads below are real captures unless explicitly marked synthetic.

BTP3 captures — Cerbo GX, MAC 00:A0:50:8D:95:69 (2025 Airstream Flying Cloud)
    Coach ID 0x699589 (bytes 8d 95 69, little-endian)
    5 active sensors cycling: 0 (Fresh), 1 (Toilet), 2 (Wash), 3 (LPG), 13 (Battery)
    Sensors 4-12 not connected to hardware — never appear in advertisements.

BTP7 capture — btmon, MAC D8:3B:DA:F8:24:06 (@atillack, GitHub issue
    TechBlueprints/victron-seelevel-python#1)
    Coach ID 0x000491 (bytes 91 04 00, little-endian)
    3 active tanks (Fresh=25%, Wash=0%, Toilet=0%), 5 disabled (code 110),
    battery 13.0 V.  Confirmed by @atillack: "hex value is 82 which is 130
    decimal - divide by 10.0 and you get the actual voltage of 13.0 V".
"""
import sys
import os
import types

sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..', 'ext'))
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..', 'ext', 'velib_python'))

# Stub modules that require system D-Bus (not available off-device).
# Import chain: seelevel_common -> ble_device -> dbus_ble_service ->
# dbus_settings_service -> vedbus -> dbus.
_stub_names = (
    'dbus', 'dbus.mainloop', 'dbus.mainloop.glib',
    'dbus.service', 'dbus.exceptions',
    'vedbus', 'settingsdevice',
    'dbus_settings_service', 'dbus_ble_service', 'dbus_role_service',
)
for _name in _stub_names:
    if _name not in sys.modules:
        sys.modules[_name] = types.ModuleType(_name)

import dbus as _dbus_stub
_dbus_stub.SystemBus = lambda **kw: None
_dbus_stub.SessionBus = lambda **kw: None
_dbus_stub.Interface = lambda *a, **kw: None
_dbus_stub.String = str
_dbus_stub.Bus = type('Bus', (), {})

import dbus_ble_service as _dbs_stub
_fake_ble_svc = type('FakeBleService', (), {
    'register_role_service': lambda self, *a: None,
    'unregister_role_service': lambda self, *a: None,
    '_get_value': lambda self, *a: 1,
})()
_dbs_stub.DbusBleService = type('DbusBleService', (), {'get': staticmethod(lambda: _fake_ble_svc)})

import dbus_role_service as _drs_stub
_drs_stub.DbusRoleService = type('DbusRoleService', (), {})

import vedbus as _vedbus_stub
_vedbus_stub.VeDbusService = type('VeDbusService', (), {})
_vedbus_stub.VeDbusItemImport = type('VeDbusItemImport', (), {})
_vedbus_stub.VeDbusItemExport = type('VeDbusItemExport', (), {})

import unittest
from ble_device_seelevel_btp3 import BleDeviceSeeLevelBTP3
from ble_device_seelevel_btp7 import BleDeviceSeeLevelBTP7


# ===================================================================
# Raw captures
# ===================================================================

# -- BTP3: Cerbo GX, MAC 00:A0:50:8D:95:69 -------------------------
#
# Coach ID = 8d 95 69  (0x699589 LE)
# Byte 3   = sensor number
# Bytes 4-6 = 3 ASCII chars (value)
# Bytes 7-9 = volume (gallons), 10-12 = capacity (gallons)
# Byte 13  = alarm ('0'-'9')

BTP3_FRESH_WATER_0PCT  = b'\x8d\x95i\x00  00000000'   # sensor 0, val "  0"
BTP3_TOILET_WATER_0PCT = b'\x8d\x95i\x01  00000000'   # sensor 1, val "  0"
BTP3_WASH_WATER_0PCT   = b'\x8d\x95i\x02  00000000'   # sensor 2, val "  0"
BTP3_LPG_OPEN          = b'\x8d\x95i\x03OPN0000000'   # sensor 3, val "OPN"
BTP3_BATTERY_13V7      = b'\x8d\x95i\r1370000000'     # sensor 13, val "137"

# -- BTP7: btmon, MAC D8:3B:DA:F8:24:06 ----------------------------
#
# Raw hex from btmon: 9104001900006e6e6e6e6e820000
#
#   91 04 00   coach ID (0x000491 LE)
#   19         Fresh  = 25%
#   00         Wash   =  0%
#   00         Toilet =  0%
#   6e         Fresh2 = 110 (tank disabled)
#   6e         Wash2  = 110
#   6e         Toilet2= 110
#   6e         Wash3  = 110
#   6e         LPG    = 110
#   82         Battery= 130 -> 13.0 V
#   00 00      unused

BTP7_CAPTURE = bytes.fromhex('9104001900006e6e6e6e6e820000')


# ===================================================================
# Helpers
# ===================================================================

class MockRoleService(dict):
    """Dict-like stand-in for DbusRoleService (no D-Bus required)."""

    def __init__(self, defaults=None):
        super().__init__(defaults or {})
        self.connected = False

    def connect(self):
        self.connected = True

    def disconnect(self):
        self.connected = False

    def get_dev_id(self):
        return 'test_dev'

    def get_dbus_id(self):
        return 'test_dev/tank'


def _make_device(cls, mac='00a0508d9569'):
    """Instantiate a SeeLevel device without D-Bus, wiring up minimal state."""
    dev = cls.__new__(cls)
    dev._role_services = {}
    dev._plog = 'test:'
    dev.info = {
        'dev_mac': mac,
        'dev_id': f'seelevel_{mac}',
        'dev_prefix': 'seelevel',
        'product_id': 0xA142,
        'product_name': cls.PRODUCT_NAME,
        'device_name': 'SeeLevel',
        'hardware_version': '1.0.0',
        'firmware_version': '1.0.0',
        'roles': dict(cls.ROLES),
        'regs': [],
        'settings': [],
        'alarms': [],
    }
    return dev


def _mock_create_service(dev, role_type, index, device_name=None, defaults=None):
    """Register a MockRoleService in the device's _role_services dict."""
    key = f'{role_type}_{index:02d}'
    svc = MockRoleService(defaults or {})
    svc._device_name = device_name
    dev._role_services[key] = svc
    return svc


# ===================================================================
# BTP3 — check_manufacturer_data
# ===================================================================

class TestBTP3CheckManufacturerData(unittest.TestCase):

    def setUp(self):
        self.dev = _make_device(BleDeviceSeeLevelBTP3)

    def test_accepts_sensor_0(self):
        self.assertTrue(self.dev.check_manufacturer_data(BTP3_FRESH_WATER_0PCT))

    def test_accepts_sensor_1(self):
        self.assertTrue(self.dev.check_manufacturer_data(BTP3_TOILET_WATER_0PCT))

    def test_accepts_sensor_2(self):
        self.assertTrue(self.dev.check_manufacturer_data(BTP3_WASH_WATER_0PCT))

    def test_accepts_sensor_3_opn(self):
        self.assertTrue(self.dev.check_manufacturer_data(BTP3_LPG_OPEN))

    def test_accepts_sensor_13_battery(self):
        self.assertTrue(self.dev.check_manufacturer_data(BTP3_BATTERY_13V7))

    def test_rejects_too_short(self):
        self.assertFalse(self.dev.check_manufacturer_data(
            BTP3_FRESH_WATER_0PCT[:6]))

    def test_rejects_unknown_sensor_14(self):
        # synthetic: sensor number 14 does not exist
        self.assertFalse(self.dev.check_manufacturer_data(
            b'\x8d\x95i\x0e0000000000'))


# ===================================================================
# BTP3 — handle_manufacturer_data
# ===================================================================

class TestBTP3HandleManufacturerData(unittest.TestCase):

    def setUp(self):
        self.dev = _make_device(BleDeviceSeeLevelBTP3)

    def _enable_sensor(self, role_type, index, defaults=None):
        return _mock_create_service(self.dev, role_type, index, defaults=defaults)

    def _patch_enabled(self):
        self.dev._is_indexed_role_enabled = lambda *a: True

    # -- Real BTP3 captures: tanks at 0% --------------------------------

    def test_fresh_water_0pct(self):
        """Real capture: sensor 0 (Fresh Water), value '  0' -> 0%."""
        svc = self._enable_sensor('tank', 0, {
            'FluidType': 1, 'Capacity': 0.0, 'Status': 0})
        self._patch_enabled()

        self.dev.handle_manufacturer_data(BTP3_FRESH_WATER_0PCT)

        self.assertEqual(svc['Level'], 0)
        self.assertEqual(svc['RawValue'], 0.0)
        self.assertEqual(svc['Remaining'], 0.0)
        self.assertEqual(svc['Status'], 0)
        self.assertTrue(svc.connected)

    def test_toilet_water_0pct(self):
        """Real capture: sensor 1 (Toilet Water), value '  0' -> 0%."""
        svc = self._enable_sensor('tank', 1, {
            'FluidType': 5, 'Capacity': 0.0, 'Status': 0})
        self._patch_enabled()

        self.dev.handle_manufacturer_data(BTP3_TOILET_WATER_0PCT)

        self.assertEqual(svc['Level'], 0)
        self.assertEqual(svc['Status'], 0)
        self.assertTrue(svc.connected)

    def test_wash_water_0pct(self):
        """Real capture: sensor 2 (Wash Water), value '  0' -> 0%."""
        svc = self._enable_sensor('tank', 2, {
            'FluidType': 2, 'Capacity': 0.0, 'Status': 0})
        self._patch_enabled()

        self.dev.handle_manufacturer_data(BTP3_WASH_WATER_0PCT)

        self.assertEqual(svc['Level'], 0)
        self.assertEqual(svc['Status'], 0)
        self.assertTrue(svc.connected)

    # -- Real BTP3 capture: OPN (disconnected) ---------------------------

    def test_lpg_opn_skipped(self):
        """Real capture: sensor 3 (LPG), value 'OPN' -> no update."""
        svc = self._enable_sensor('tank', 3, {
            'FluidType': 8, 'Capacity': 0.0, 'Status': 0})
        self._patch_enabled()

        self.dev.handle_manufacturer_data(BTP3_LPG_OPEN)

        self.assertNotIn('Level', svc)
        self.assertFalse(svc.connected)

    # -- Real BTP3 capture: battery voltage ------------------------------

    def test_battery_13v7(self):
        """Real capture: sensor 13, value '137' -> 13.7 V."""
        svc = self._enable_sensor('battery', 13, {'Status': 0})
        self._patch_enabled()

        self.dev.handle_manufacturer_data(BTP3_BATTERY_13V7)

        self.assertAlmostEqual(svc['/Dc/0/Voltage'], 13.7, places=1)
        self.assertEqual(svc['Status'], 0)
        self.assertTrue(svc.connected)

    # -- Real capture: alarm byte ----------------------------------------

    def test_alarm_byte_zero_from_real_capture(self):
        """Real capture: all captured payloads have alarm byte '0' (0x30)."""
        svc = self._enable_sensor('tank', 0, {
            'FluidType': 1, 'Capacity': 0.0, 'Status': 0})
        self._patch_enabled()

        self.dev.handle_manufacturer_data(BTP3_FRESH_WATER_0PCT)

        self.assertEqual(svc['/Alarms/Low/State'], 0)

    # -- Synthetic: cases not covered by real hardware -------------------

    def test_err_sets_error_status(self):
        """Synthetic: sensor 0, value 'ERR' -> Status 5."""
        svc = self._enable_sensor('tank', 0, {
            'FluidType': 1, 'Capacity': 0.0, 'Status': 0})
        self._patch_enabled()

        self.dev.handle_manufacturer_data(b'\x8d\x95i\x00ERR0000000')

        self.assertEqual(svc['Status'], 5)
        self.assertTrue(svc.connected)

    def test_tank_50pct_with_capacity(self):
        """Synthetic: sensor 0 at 50%, capacity 0.2 m3 -> remaining 0.1."""
        svc = self._enable_sensor('tank', 0, {
            'FluidType': 1, 'Capacity': 0.2, 'Status': 0})
        self._patch_enabled()

        self.dev.handle_manufacturer_data(b'\x8d\x95i\x00 500000000')

        self.assertEqual(svc['Level'], 50)
        self.assertAlmostEqual(svc['Remaining'], 0.1, places=3)

    def test_tank_clamps_to_100(self):
        """Synthetic: value > 100 is clamped to 100%."""
        svc = self._enable_sensor('tank', 0, {
            'FluidType': 1, 'Capacity': 0.0, 'Status': 0})
        self._patch_enabled()

        self.dev.handle_manufacturer_data(b'\x8d\x95i\x001200000000')

        self.assertEqual(svc['Level'], 100)

    def test_alarm_nonzero(self):
        """Synthetic: alarm byte '3' -> low alarm active."""
        svc = self._enable_sensor('tank', 0, {
            'FluidType': 1, 'Capacity': 0.0, 'Status': 0})
        self._patch_enabled()

        self.dev.handle_manufacturer_data(b'\x8d\x95i\x00 100000003')

        self.assertEqual(svc['/Alarms/Low/State'], 1)

    def test_temperature_72f(self):
        """Synthetic: sensor 7 (Temp), value '072' (72 degF) -> 22.2 degC."""
        svc = self._enable_sensor('temperature', 7, {'Status': 0})
        self._patch_enabled()

        self.dev.handle_manufacturer_data(b'\x8d\x95i\x070720000000')

        self.assertAlmostEqual(svc['Temperature'], 22.2, places=1)
        self.assertEqual(svc['Status'], 0)
        self.assertTrue(svc.connected)

    def test_temperature_32f_freezing(self):
        """Synthetic: sensor 7, value '032' (32 degF) -> 0.0 degC."""
        svc = self._enable_sensor('temperature', 7, {'Status': 0})
        self._patch_enabled()

        self.dev.handle_manufacturer_data(b'\x8d\x95i\x070320000000')

        self.assertAlmostEqual(svc['Temperature'], 0.0, places=1)

    def test_battery_low_voltage(self):
        """Synthetic: sensor 13, value '108' -> 10.8 V."""
        svc = self._enable_sensor('battery', 13, {'Status': 0})
        self._patch_enabled()

        self.dev.handle_manufacturer_data(b'\x8d\x95i\r1080000000')

        self.assertAlmostEqual(svc['/Dc/0/Voltage'], 10.8, places=1)

    def test_unparseable_value_ignored(self):
        """Synthetic: non-numeric, non-OPN/ERR data -> no crash."""
        svc = self._enable_sensor('tank', 0, {
            'FluidType': 1, 'Capacity': 0.0, 'Status': 0})
        self._patch_enabled()

        self.dev.handle_manufacturer_data(b'\x8d\x95i\x00XYZ0000000')

        self.assertNotIn('Level', svc)

    # -- Lazy service creation (uses real capture to trigger) ------------

    def test_lazy_creates_tank_service(self):
        """Real capture triggers lazy creation of tank service."""
        self._patch_enabled()
        created = {}

        def mock_create(role_type, index, device_name=None):
            svc = _mock_create_service(self.dev, role_type, index,
                                       device_name=device_name,
                                       defaults={'FluidType': 0, 'Capacity': 0.2, 'Status': 0})
            created[f'{role_type}_{index:02d}'] = svc
            return svc

        self.dev._create_indexed_role_service = mock_create
        self.dev.handle_manufacturer_data(BTP3_FRESH_WATER_0PCT)

        self.assertIn('tank_00', created)
        self.assertEqual(created['tank_00']._device_name, 'SeeLevel Fresh Water')

    def test_lazy_sets_fluid_type(self):
        """Lazy creation sets FluidType from SENSORS table."""
        self._patch_enabled()

        def mock_create(role_type, index, device_name=None):
            return _mock_create_service(self.dev, role_type, index,
                                       defaults={'FluidType': 0, 'Capacity': 0.2, 'Status': 0})

        self.dev._create_indexed_role_service = mock_create
        self.dev.handle_manufacturer_data(BTP3_TOILET_WATER_0PCT)

        svc = self.dev._role_services['tank_01']
        self.assertEqual(svc['FluidType'], 5)  # Black water

    def test_lazy_zeroes_default_capacity(self):
        """Lazy creation resets upstream default capacity 0.2 -> 0.0."""
        self._patch_enabled()

        def mock_create(role_type, index, device_name=None):
            return _mock_create_service(self.dev, role_type, index,
                                       defaults={'FluidType': 0, 'Capacity': 0.2, 'Status': 0})

        self.dev._create_indexed_role_service = mock_create
        self.dev.handle_manufacturer_data(BTP3_FRESH_WATER_0PCT)

        svc = self.dev._role_services['tank_00']
        self.assertEqual(svc['Capacity'], 0.0)

    # -- Disabled sensor -------------------------------------------------

    def test_disabled_sensor_not_updated(self):
        """Real capture, but sensor disabled -> no D-Bus update."""
        svc = self._enable_sensor('tank', 0, {
            'FluidType': 1, 'Capacity': 0.0, 'Status': 0})
        self.dev._is_indexed_role_enabled = lambda *a: False

        self.dev.handle_manufacturer_data(BTP3_FRESH_WATER_0PCT)

        self.assertNotIn('Level', svc)
        self.assertFalse(svc.connected)


# ===================================================================
# BTP7 — check_manufacturer_data
# ===================================================================

class TestBTP7CheckManufacturerData(unittest.TestCase):

    def setUp(self):
        self.dev = _make_device(BleDeviceSeeLevelBTP7, 'd83bdaf82406')

    def test_accepts_real_capture(self):
        """Real btmon capture: 14 bytes."""
        self.assertTrue(self.dev.check_manufacturer_data(BTP7_CAPTURE))

    def test_accepts_minimum_12_bytes(self):
        """12 bytes is the minimum for tank + battery."""
        self.assertTrue(self.dev.check_manufacturer_data(BTP7_CAPTURE[:12]))

    def test_rejects_11_bytes(self):
        """11 bytes is too short (missing battery byte)."""
        self.assertFalse(self.dev.check_manufacturer_data(BTP7_CAPTURE[:11]))


# ===================================================================
# BTP7 — handle_manufacturer_data (real capture)
# ===================================================================

class TestBTP7HandleManufacturerData(unittest.TestCase):
    """
    All tests in this class use the real btmon capture from @atillack
    (GitHub TechBlueprints/victron-seelevel-python#1) unless marked synthetic.
    """

    def setUp(self):
        self.dev = _make_device(BleDeviceSeeLevelBTP7, 'd83bdaf82406')
        self.dev._is_indexed_role_enabled = lambda *a: True

        for slot in range(8):
            name, fluid = BleDeviceSeeLevelBTP7.TANK_SLOTS[slot]
            _mock_create_service(self.dev, 'tank', slot, device_name=f'SeeLevel {name}',
                                 defaults={'FluidType': fluid, 'Capacity': 0.0, 'Status': 0})
        _mock_create_service(self.dev, 'battery', 8, device_name='SeeLevel Voltage',
                             defaults={'Status': 0})

    # -- Active tanks from real capture ----------------------------------

    def test_fresh_water_25pct(self):
        """Byte 3 = 0x19 = 25 -> Fresh Water at 25%."""
        self.dev.handle_manufacturer_data(BTP7_CAPTURE)

        svc = self.dev._role_services['tank_00']
        self.assertEqual(svc['Level'], 25)
        self.assertEqual(svc['RawValue'], 25.0)
        self.assertEqual(svc['Remaining'], 0.0)
        self.assertTrue(svc.connected)

    def test_wash_water_0pct(self):
        """Byte 4 = 0x00 -> Wash Water at 0%."""
        self.dev.handle_manufacturer_data(BTP7_CAPTURE)

        svc = self.dev._role_services['tank_01']
        self.assertEqual(svc['Level'], 0)
        self.assertTrue(svc.connected)

    def test_toilet_water_0pct(self):
        """Byte 5 = 0x00 -> Toilet Water at 0%."""
        self.dev.handle_manufacturer_data(BTP7_CAPTURE)

        svc = self.dev._role_services['tank_02']
        self.assertEqual(svc['Level'], 0)
        self.assertTrue(svc.connected)

    # -- Disabled tanks from real capture (code 110) ---------------------

    def test_fresh2_disabled(self):
        """Byte 6 = 0x6e = 110 (tank disabled) -> error status."""
        self.dev.handle_manufacturer_data(BTP7_CAPTURE)
        self.assertEqual(self.dev._role_services['tank_03']['Status'], 5)

    def test_wash2_disabled(self):
        """Byte 7 = 0x6e = 110 (tank disabled) -> error status."""
        self.dev.handle_manufacturer_data(BTP7_CAPTURE)
        self.assertEqual(self.dev._role_services['tank_04']['Status'], 5)

    def test_toilet2_disabled(self):
        """Byte 8 = 0x6e = 110 (tank disabled) -> error status."""
        self.dev.handle_manufacturer_data(BTP7_CAPTURE)
        self.assertEqual(self.dev._role_services['tank_05']['Status'], 5)

    def test_wash3_disabled(self):
        """Byte 9 = 0x6e = 110 (tank disabled) -> error status."""
        self.dev.handle_manufacturer_data(BTP7_CAPTURE)
        self.assertEqual(self.dev._role_services['tank_06']['Status'], 5)

    def test_lpg_disabled(self):
        """Byte 10 = 0x6e = 110 (tank disabled) -> error status."""
        self.dev.handle_manufacturer_data(BTP7_CAPTURE)
        self.assertEqual(self.dev._role_services['tank_07']['Status'], 5)

    # -- Battery voltage from real capture -------------------------------

    def test_battery_13v0(self):
        """Byte 11 = 0x82 = 130 -> 13.0 V (confirmed by @atillack)."""
        self.dev.handle_manufacturer_data(BTP7_CAPTURE)

        svc = self.dev._role_services['battery_08']
        self.assertAlmostEqual(svc['/Dc/0/Voltage'], 13.0, places=1)
        self.assertEqual(svc['Status'], 0)
        self.assertTrue(svc.connected)

    # -- Synthetic: edge cases -------------------------------------------

    def test_tank_with_capacity(self):
        """Synthetic: slot 0 at 40%, capacity 0.3 m3 -> remaining 0.12."""
        self.dev._role_services['tank_00']['Capacity'] = 0.3

        payload = bytearray(BTP7_CAPTURE)
        payload[3] = 40
        self.dev.handle_manufacturer_data(bytes(payload))

        svc = self.dev._role_services['tank_00']
        self.assertEqual(svc['Level'], 40)
        self.assertAlmostEqual(svc['Remaining'], 0.12, places=3)

    def test_all_tanks_100pct(self):
        """Synthetic: all 8 tanks at exactly 100%."""
        payload = bytearray(14)
        payload[0:3] = b'\x91\x04\x00'
        for i in range(8):
            payload[3 + i] = 100
        payload[11] = 130

        self.dev.handle_manufacturer_data(bytes(payload))

        for slot in range(8):
            svc = self.dev._role_services[f'tank_{slot:02d}']
            self.assertEqual(svc['Level'], 100,
                             f'slot {slot} should be 100%')

    def test_battery_low_voltage(self):
        """Synthetic: byte 11 = 110 -> 11.0 V."""
        payload = bytearray(BTP7_CAPTURE)
        payload[11] = 110
        self.dev.handle_manufacturer_data(bytes(payload))

        svc = self.dev._role_services['battery_08']
        self.assertAlmostEqual(svc['/Dc/0/Voltage'], 11.0, places=1)

    def test_battery_disabled_not_updated(self):
        """Battery slot disabled -> no voltage update."""
        self.dev._is_indexed_role_enabled = lambda rt, idx: rt == 'tank'

        self.dev.handle_manufacturer_data(BTP7_CAPTURE)

        svc = self.dev._role_services['battery_08']
        self.assertNotIn('/Dc/0/Voltage', svc)
        self.assertFalse(svc.connected)

    def test_short_payload_battery_safe(self):
        """11-byte payload: tanks update, battery skipped (< 12 bytes)."""
        self.dev.handle_manufacturer_data(BTP7_CAPTURE[:11])

        svc = self.dev._role_services['battery_08']
        self.assertNotIn('/Dc/0/Voltage', svc)


# ===================================================================
# Sensor mapping consistency
# ===================================================================

class TestSensorMappings(unittest.TestCase):

    def test_btp3_sensor_count(self):
        self.assertEqual(len(BleDeviceSeeLevelBTP3.SENSORS), 14)

    def test_btp3_all_sensors_have_role(self):
        for num, (name, role_type, _) in BleDeviceSeeLevelBTP3.SENSORS.items():
            self.assertIn(role_type, ('tank', 'temperature', 'battery'),
                          f'sensor {num} ({name}) has unexpected role {role_type!r}')

    def test_btp3_battery_is_sensor_13(self):
        name, role_type, _ = BleDeviceSeeLevelBTP3.SENSORS[13]
        self.assertEqual(role_type, 'battery')

    def test_btp7_tank_slot_count(self):
        self.assertEqual(len(BleDeviceSeeLevelBTP7.TANK_SLOTS), 8)

    def test_btp7_roles_include_battery(self):
        self.assertIn('battery', BleDeviceSeeLevelBTP7.ROLES)

    def test_btp3_roles_include_all_three(self):
        for role in ('tank', 'temperature', 'battery'):
            self.assertIn(role, BleDeviceSeeLevelBTP3.ROLES)


if __name__ == '__main__':
    unittest.main()
