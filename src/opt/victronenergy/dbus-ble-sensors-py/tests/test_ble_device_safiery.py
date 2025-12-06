import sys
import os
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..', 'ext'))
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..', 'ext', 'velib_python'))
from ble_device_base_tests import BleDeviceBaseTests
from ble_device_safiery import BleDeviceSafiery


class BleDeviceSafieryTests(BleDeviceBaseTests):
    # To be executed with command : python3 -m unittest test_ble_device_safiery.py

    def setUp(self):
        super().setUp(BleDeviceSafiery, '012345332211')

    def test_nic_mismatch(self):
        raw = b'\x0A\x64\xB2\x2C\x01\x00\x00\x00\xFE\x05'
        self.device.configure(raw)
        self.device._load_configuration()
        self.assertFalse(self.device.check_manufacturer_data(raw))

    def test_parsing_1(self):
        # 0A 64 B2 2C 01 33 22 11 FE 05
        #
        # 0A    : HardwareID, Bits 7 => 10
        # 64    : BatteryVoltage, (MSB)0110 0100(LSB), Bits 7 => (MSB)110 0100(LSB) = 100, Scale 32 => 100/32=3.125
        # B2    : Temperature, (MSB)1011 0010(LSB), Bits 7 => (MSB)011 0010(LSB) = 50, Scale 1, Bias -40 => 50/1 - 40 = 10
        # B2    : SyncButton, (MSB)1011 0010(LSB), Shift 7 => (MSB)0000 0001(LSB), Bits 1 => 1
        # 2C01  : RawValue, 012C=(MSB)0000 0001 0010 1100(LSB), Bits 14 => (MSB)00 0001 0010 1100(LSB) = 300, Scale 10 => 300/10 = 30
        # FE    : AccelX, -2, Scale 1024 => -2/1024 = -0.001953125
        # 05    : AccelY, 5, Scale 1024 => 5/1024 = 0.0048828125
        # ??    : AccelZ, None

        self._test_parsing(
            b'\x0A\x64\xB2\x2C\x01\x33\x22\x11\xFE\x05',
            {
                'tank': {
                    'HardwareID': 10,
                    'BatteryVoltage': 3.125,
                    'Temperature': 10.0,
                    'SyncButton': 1,
                    'RawValue': 30.0,
                    'AccelX': -0.001953125,
                    'AccelY': 0.0048828125,
                    # 'AccelZ': None, : Incoherent definition in the original C class...
                }
            }
        )
