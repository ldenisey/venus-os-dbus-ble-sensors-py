import sys
import os
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..', 'ext'))
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..', 'ext', 'velib_python'))
from ble_device_gobius import BleDeviceGobius
from ble_device_base_tests import BleDeviceBaseTests


class BleDeviceGobiusTest(BleDeviceBaseTests):
    # To be executed with command : python3 -m unittest test_ble_device_gobius.py

    def setUp(self):
        super().setUp(BleDeviceGobius, '012345678901')

    def test_gobius_level(self):
        self.assertEqual(self.device.gobius_level(0xffff), -1)
        self.assertEqual(self.device.gobius_level(0xfffe), -1)
        self.assertEqual(self.device.gobius_level(150), 15.0)

    def test_parsing_1(self):
        # 05 3C 96 00 43 59 01 01 01 02 09 00 00 00
        #
        # 05     : HardwareID, (MSB)0000 0101(LSB), Bits 7 => (MSB)000 0101(LSB) = 5
        # 3C     : Temperature, (MSB)0011 1100(LSB), Bits 7 => (MSB)011 1100(LSB) = 60, Scale 1, Bias -40 => 60/1 - 40 = 20
        # 9600   : RawValue, (MSB)0000 0000 1001 0110(LSB) = 150, Xlate => 150/10 = 15
        # 678901 : Mac NIC
        # 010102 : Firmware version => 01.01.02
        # 09     : Status Flags
        # 000000 : Spare
        self._test_parsing(
            b'\x05\x3C\x96\x00\x67\x89\x01\x01\x01\x02\x09\x00\x00\x00',
            {
                'tank': {
                    'HardwareID': 5,
                    'Temperature': 20.0,
                    'RawValue': 15.0,
                }
            }
        )
        self.assertEqual('1.1.2', self.device.info['firmware_version'])
