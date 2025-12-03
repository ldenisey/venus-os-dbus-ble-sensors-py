import sys
import os
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..', 'ext'))
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..', 'ext', 'velib_python'))
from ble_device_teltonika import BleDeviceTeltonika
from ble_device_base_tests import BleDeviceBaseTests


class BleDeviceTeltonikaTests(BleDeviceBaseTests):
    # To be executed with command : python3 -m unittest test_ble_device_teltonika.py

    def setUp(self):
        super().setUp(BleDeviceTeltonika, '7cd9f411427d', 'PITCH_ROLL')

    # @unittest.skip("Temporarily disabled for debugging")
    def test_spec_example(self):
        # Cf. https://wiki.teltonika-gps.com/view/EYE_SENSOR_/_BTSMP1#EYE_Sensor_Bluetooth%C2%AE_frame_parsing_example

        raw_data = b'\x01\xb7\x08\xb4\x12\x0c\xcb\x0b\xff\xc7\x67'
        #   01: Protocol version
        #   B7: Flags: B7=(MSB)1011 0111(LSB) => Bat volt on, low bat False, Angles on, Counter on, Mag state False, Mag on, Humidity on, Temp on
        #   08
        #   B4: Temperature: 08B4 = 2228, 2228 / 100 = 22.28°C
        #   12: Humidity: 12 = 18%
        #   0C
        #   CB: Counter: 0CCB=(MSB)0000 1100 1100 1011(LSB) => 0@MSB=Moving False, 000 1100 1100 1011=3275 moves
        #   0B: Pitch: 0B=11°
        #   FF
        #   C7: Roll: FFC7=-57°
        #   67: Battery voltage: 67=103, 2000 + (103 * 10) = 3030mV
        expected_dict = {
            'movement': {
                'LowBattery': 0,
                'MovementState': 0,
                'MovementCount': 3275,
                'Pitch': 11,
                'Roll': -57,
                'BatteryVoltage': 3030.0
            },
            'temperature': {
                'LowBattery': 0,
                'Temperature': 22.28,
                'Humidity': 18,
                'BatteryVoltage': 3030.0
            },
            'digitalinput': {
                'InputState': 0,
                'LowBattery': 0,
                'BatteryVoltage': 3030.0
            }
        }
        self._test_parsing(raw_data, expected_dict)

    # @unittest.skip("Temporarily disabled for debugging")
    def test_sensor_1(self):
        raw_data = b'\x01\xbf\x06\xe6:\xe5g\xf9\x00zM'
        #   01: Protocol version
        #   BF: Flags: BF=(MSB)1011 1111(LSB) => Bat volt on, low bat False, Angles on, Counter on, Mag state True, Mag on, Humidity on, Temp on
        #   06
        #   E6: Temperature: 06E6 = 1766, 1766 / 100 = 17.66°C
        # :=3A: Humidity: 3A = 58%
        #   E5
        # g=67: Counter: E567=(MSB)1110 0101  0110 0111(LSB) => 1@MSB=Moving True, 110 0101  0110 0111=25959 moves
        #   F9: Pitch: F9=-7°
        #   00
        # z=7A: Roll: 007A=122
        # M=4D: Battery voltage: 4D=77, 2000 + (77 * 10) = 2770mV
        expected_dict = {
            'movement': {
                'LowBattery': 0,
                'MovementState': 1,
                'MovementCount': 25959,
                'Pitch': -7,
                'Roll': 122,
                'BatteryVoltage': 2770.0
            },
            'temperature': {
                'LowBattery': 0,
                'Temperature': 17.66,
                'Humidity': 58,
                'BatteryVoltage': 2770.0
            },
            'digitalinput': {
                'InputState': 1,
                'LowBattery': 0,
                'BatteryVoltage': 2770.0
            }
        }
        self._test_parsing(raw_data, expected_dict)

    # @unittest.skip("Temporarily disabled for debugging")
    def test_sensor_2(self):
        raw_data = b'\x01\xd3\x06\xe6:\x65gM'
        #   01: Protocol version
        #   D3: Flags: D3=(MSB)1101 0011(LSB) => Bat volt on, low bat True, Angles off, Counter on, Mag state False, Mag off, Humidity on, Temp on
        #   06
        #   E6: Temperature: 06E6 = 1766, 1766 / 100 = 17.66°C
        # :=3A: Humidity: 3A = 58%
        #   65
        # g=67: Counter: 6567=(MSB)0110 0101  0110 0111(LSB) => 0@MSB=Moving False, 110 0101  0110 0111=25959 moves
        # M=4D: Battery voltage: 4D=77, 2000 + (77 * 10) = 2770mV
        expected_dict = {
            'temperature': {
                'LowBattery': 1,
                'Temperature': 17.66,
                'Humidity': 58,
                'BatteryVoltage': 2770.0
            },
            'movement': {
                'LowBattery': 1,
                'MovementState': 0,
                'MovementCount': 25959,
                'BatteryVoltage': 2770.0
            }
        }
        self._test_parsing(raw_data, expected_dict)

    # @unittest.skip("Temporarily disabled for debugging")
    def test_sensor_3(self):
        raw_data = b'\x01\x8C\x67'
        #   01: Protocol version
        #   8C: Flags: 8C=(MSB)1000 1100(LSB) => Bat volt on, low bat False, Angles off, Counter off, Mag state True, Mag on, Humidity off, Temp off
        #   67: Battery voltage: 67=103, 2000 + (103 * 10) = 3030mV
        expected_dict = {
            'digitalinput': {
                'InputState': 1,
                'LowBattery': 0,
                'BatteryVoltage': 3030
            }
        }
        self._test_parsing(raw_data, expected_dict)

    # @unittest.skip("Temporarily disabled for debugging")
    def test_beacon(self):
        raw_data = b'\x01\xC0\x4D'
        #   01: Protocol version
        #   C0: Flags: C0=(MSB)1100 0000(LSB) => Bat volt on, low bat True, Angles off, Counter off, Mag state False, Mag off, Humidity off, Temp off
        # M=4D: Battery voltage: 4D=77, 2000 + (77 * 10) = 2770mV
        expected_dict = {}  # No roles, thus the device
        with self.assertRaises(ValueError) as e:
            self._test_parsing(raw_data, expected_dict)
        self.assertEqual(str(e.exception),
                         "7cd9f411427d - PITCH_ROLL 427D: Configuration 'roles' must have at least one element")
