import sys
import os
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..', 'ext'))
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..', 'ext', 'velib_python'))
import logging
import unittest
from ble_role_tank import BleRoleTank


class TestBleRoleTank(unittest.TestCase):
    # To be executed with command : python3 -m unittest test_ble_role_tank.py

    def setUp(self):
        logging.basicConfig(level=logging.DEBUG)
        self.maxDiff = None  # See full comparison on failures
        # Default bottom-up tank
        self.tank = BleRoleTank(config={'flags': []})
        self.tank.check_configuration()
        # Role service mock with sensible defaults
        self.dbus_role_service = {
            'RawValue': 0.0,
            'RawValueEmpty': 0.0,
            'RawValueFull': 100.0,
            'Capacity': 100.0,
            'Shape': '',
            '/Alarms/High/Enable': 0,
            '/Alarms/High/Active': 90,
            '/Alarms/High/Restore': 80,
            '/Alarms/High/State': 0,
            '/Alarms/Low/Enable': 0,
            '/Alarms/Low/Active': 10,
            '/Alarms/Low/Restore': 15,
            '/Alarms/Low/State': 0,
            'Level': 0.0,
            'Remaining': 0.0,
        }

    def test_parse_shape_valid(self):
        # 25%->20%, 50%->45%, 75%->70% volume mapping
        self.tank._parse_shape_str("25:20,50:45,75:70")
        self.assertIsNotNone(self.tank._shape_map)
        self.assertEqual(len(self.tank._shape_map), 5)
        self.assertEqual(self.tank._shape_map, [(0.0, 0.0), (0.25, 0.2), (0.5, 0.45), (0.75, 0.7), (1.0, 1.0)])

    def test_parse_shape_invalid_elements(self):
        self.tank._parse_shape_str("25:20,50")  # missing second element
        self.assertEqual(self.tank._shape_map, [])

        self.tank._parse_shape_str("25:abc")  # non-integer
        self.assertEqual(self.tank._shape_map, [])

        self.tank._parse_shape_str("0:10")  # out of range
        self.assertEqual(self.tank._shape_map, [])

        self.tank._parse_shape_str("25:20,24:22")  # not strictly increasing
        self.assertEqual(self.tank._shape_map, [])

    def test_compute_level_linear_no_shape(self):
        # No shape set -> linear mapping
        self.tank._shape_map = []
        level, remaining, status = self.tank._compute_level(
            rawValue=50.0, empty=0.0, full=100.0, capacity=200.0
        )
        self.assertEqual(level, 50)          # 50%
        self.assertAlmostEqual(remaining, 100.0, places=6)  # 50% of 200
        self.assertEqual(status, 0)

    def test_compute_level_with_shape_interpolation(self):
        # Shape: 0->0, 50% level -> 40% volume, 100% -> 100%
        self.tank._shape_map = [(0, 0), (0.5, 0.4), (1.0, 1.0)]
        level, remaining, status = self.tank._compute_level(
            rawValue=50.0, empty=0.0, full=100.0, capacity=100.0
        )
        self.assertEqual(level, 40)          # mapped to 40%
        self.assertAlmostEqual(remaining, 40.0, places=6)
        self.assertEqual(status, 0)

    def test_compute_level_below_empty(self):
        self.tank._shape_map = [(0, 0), (1.0, 1.0)]
        # Below empty
        level, remaining, status = self.tank._compute_level(
            rawValue=-10.0, empty=0.0, full=100.0, capacity=100.0
        )
        self.assertEqual(level, 0)
        self.assertEqual(remaining, 0.0)

    def test_compute_level_above_full(self):
        self.tank._shape_map = [(0, 0)]
        # Above full
        level, remaining, status = self.tank._compute_level(
            rawValue=120.0, empty=0.0, full=100.0, capacity=100.0
        )
        self.assertEqual(level, 100)
        self.assertEqual(remaining, 100.0)

    def test_compute_level_invalid_params(self):
        self.tank._shape_map = [(1.0, 1.0)]
        # Missing values
        self.assertEqual(self.tank._compute_level(None, 0, 100, 100), (None, None, 4))

        # Empty >= Full for bottom-up -> error
        self.assertEqual(self.tank._compute_level(50, 100, 0, 100), (None, None, 4))

        # Topdown: empty must be > full
        topdown = BleRoleTank(config={'flags': ['TANK_FLAG_TOPDOWN']})
        topdown._shape_map = [(0, 0), (1.0, 1.0)]
        self.assertEqual(topdown._compute_level(50, 0, 100, 100), (None, None, 4))

    def test_update_data_parses_shape_once(self):
        self.dbus_role_service['Shape'] = "25:20,50:45,75:70"
        sensor = {'RawValue': 25.0}

        # First call parses shape
        self.tank.update_data(self.dbus_role_service, sensor)
        self.assertIn('Level', sensor)
        self.assertEqual(sensor['Level'], 20)
        self.assertIn('Remaining', sensor)
        self.assertEqual(sensor['Remaining'], 20.0)

        # Second call uses cached _shape_map
        sensor2 = {'RawValue': 75.0}
        self.tank.update_data(self.dbus_role_service, sensor2)
        self.assertIn('Level', sensor2)
        self.assertEqual(sensor2['Level'], 70)
        self.assertIn('Remaining', sensor2)
        self.assertEqual(sensor2['Remaining'], 70.0)

    def test_alarms_high_low(self):
        # Set level higher than high alarm active threshold
        self.dbus_role_service['Level'] = 95
        # High alarm disabled
        self.dbus_role_service['/Alarms/High/Enable'] = 0
        self.assertEqual(self.tank.get_alarm_high_state(self.dbus_role_service), 0)
        # Enable alarm, state is 0, high threshold is 90
        self.dbus_role_service['/Alarms/High/Enable'] = 1
        self.assertEqual(self.tank.get_alarm_high_state(self.dbus_role_service), 1)  # Alarm on
        # When alarm is active, use Restore threshold
        self.dbus_role_service['/Alarms/High/State'] = 1
        self.dbus_role_service['Level'] = 85
        self.assertEqual(self.tank.get_alarm_high_state(self.dbus_role_service), 1)
        # Silent alarm when level goes below restore threshold
        self.dbus_role_service['Level'] = 79
        self.assertEqual(self.tank.get_alarm_high_state(self.dbus_role_service), 0)
        self.dbus_role_service['/Alarms/High/State'] = 0

        # Low alarm tests
        self.dbus_role_service['Level'] = 9
        self.dbus_role_service['/Alarms/Low/Enable'] = 1
        self.assertEqual(self.tank.get_alarm_low_state(self.dbus_role_service), 1)
        self.dbus_role_service['/Alarms/Low/State'] = 1  # use Restore
        self.assertEqual(self.tank.get_alarm_low_state(self.dbus_role_service), 1)
        self.dbus_role_service['Level'] = 16
        self.assertEqual(self.tank.get_alarm_low_state(self.dbus_role_service), 0)

    def test_topdown_behavior(self):
        topdown = BleRoleTank(config={'flags': ['TANK_FLAG_TOPDOWN']})
        topdown._shape_map = [(0, 0), (1.0, 1.0)]
        # Valid: empty > full
        level, remaining, status = topdown._compute_level(
            rawValue=50.0, empty=100.0, full=0.0, capacity=100.0
        )
        # For raw 50 between empty 100 and full 0, level should be 50%
        self.assertEqual(level, 50)
        self.assertAlmostEqual(remaining, 50.0, places=6)
        self.assertEqual(status, 0)
