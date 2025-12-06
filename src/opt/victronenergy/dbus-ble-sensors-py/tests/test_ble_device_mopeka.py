import sys
import os
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..', 'ext'))
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..', 'ext', 'velib_python'))
from ble_device_base_tests import BleDeviceBaseTests
from ble_device_mopeka import BleDeviceMopeka


class BleDeviceSafieryTests(BleDeviceBaseTests):
    # To be executed with command : python3 -m unittest test_ble_device_mopeka.py

    def setUp(self):
        super().setUp(BleDeviceMopeka, '012345112233')

    def test_nic_mismatch(self):
        raw = b'\x03\x64\x3C\x88\x53\x11\x00\x33\xF4\x08'
        self.device.configure(raw)
        self.device._load_configuration()
        self.assertFalse(self.device.check_manufacturer_data(raw))

    def test_parsing_1(self):
        # 03 64 3C 88 53 11 22 33 F4 08
        #
        # 03    : HardwareID, Bits 7 => 3
        # 03    : TankLevelExtension, Bits 1, Shift 7 => 0
        # 64    : BatteryVoltage, (MSB)0110 0100(LSB), Bits 7 => (MSB)110 0100(LSB) = 100, Scale 32 => 100/32=3.125
        # 3C    : Temperature, (MSB)0011 1100(LSB), Bits 7 => (MSB)011 1100(LSB) = 60, Scale 1, Bias -40 => 60/1 - 40 = 20
        # 3C    : SyncButton, (MSB)0011 1100(LSB), Shift 7, bits 1 => 0
        # 8853  : RawValue, 5388=(MSB)0101 0011 1000 1000(LSB), Bits 14 => (MSB)01 0011 1000 1000(LSB) = 5000
        # 53    : Quality, 53=(MSB)0101 0011(LSB), Shift 6 => (MSB)01(LSB), Bits 2 => 1
        # F4    : AccelX, -12, Scale 1024 => -12/1024 = -0.01171875
        # 08    : AccelY, 8, Scale 1024 => 8/1024 = 0.0078125

        self._test_parsing(
            b'\x03\x64\x3C\x88\x53\x11\x22\x33\xF4\x08',
            {
                'tank': {
                    'HardwareID': 3,
                    'TankLevelExtension': 0,
                    'BatteryVoltage': 3.125,
                    'RawValue': 5000,
                },
                "temperature": {
                    'HardwareID': 3,
                    'BatteryVoltage': 3.125,
                    'Temperature': 20.0,
                },
                "movement": {
                    'HardwareID': 3,
                    'BatteryVoltage': 3.125,
                    'AccelX': -0.01171875,
                    'AccelY': 0.0078125,
                }
            }
        )
