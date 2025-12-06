import sys
import os
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..', 'ext'))
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..', 'ext', 'velib_python'))
from ble_device_base_tests import BleDeviceBaseTests
from ble_device_ruuvi import BleDeviceRuuvi


class BleDeviceRuuviTests(BleDeviceBaseTests):
    # To be executed with command : python3 -m unittest test_ble_device_ruuvi.py

    def setUp(self):
        super().setUp(BleDeviceRuuvi, '012345332211')

    def test_parsing_1(self):
        # 05 11 94 55 A8 C8 7D 00 64 FF 9C 00 00 05 78 10 12 34 56 78 9A BC DE F0
        #
        # 05    : Device type
        # 1194  : Temperature, (MSB)0001 0001 1001 0100(LSB) = 4500, Scale 200 => 4500/200 = 22.5
        # 55A8  : Humidity, (MSB)0101 0101 1010 1000(LSB) = 21928, Scale 400 => 21928/400 = 54.82
        # C87D  : Pressure, (MSB)1100 1000 0111 1101(LSB) = 51325, Scale 100, Bias 500 => 51325/100 + 500 = 1013.25
        # 0064  : AccelX, (MSB)0000 0000 0110 0100(LSB) = 100, Scale 1000 => 100/1000 = 0.1
        # FF9C  : AccelY, (MSB)1111 1111 1001 1100(LSB) = -100, Scale 1000 => -100/1000 = -0.1
        # 0000  : AccelZ, (MSB)0000 0000 0000 0000(LSB) = 0, Scale 1000 => 0/1000 = 0.0
        # 0578  : BatteryVoltage, (MSB)0000 0101 0111 1000(LSB), Shift 5, Bits 11 => (MSB)000 0010 1011(LSB), Scale 1000, Bias 1.6 => 43/1000 + 1.6 = 1.643
        # 78    : TxPower, (MSB)0111 1000(LSB), Bits 5 => (MSB)1 1000(LSB) = 24, Scale 0.5, Bias -40 => 24/0.5 - 40 = 8.0
        # 1234  : SeqNo, (MSB)0001 0010 0011 0100(LSB) = 4660

        self._test_parsing(
            b'\x05\x11\x94\x55\xA8\xC8\x7D\x00\x64\xFF\x9C\x00\x00\x05\x78\x10\x12\x34\x56\x78\x9A\xBC\xDE\xF0',
            {
                'temperature': {
                    'Temperature': 22.5,
                    'Humidity': 54.82,
                    'Pressure': 1013.25,
                    'BatteryVoltage': 1.643,
                    'TxPower': 8.0,
                    'SeqNo': 4660,
                },
                'movement': {
                    'AccelX': 0.1,
                    'AccelY': -0.1,
                    'AccelZ': 0.0,
                    'BatteryVoltage': 1.643,
                    'TxPower': 8.0,
                    'SeqNo': 4660,
                }
            }
        )

    def test_parsing_2(self):
        # 06 0F A0 55 A8 C8 7D 00 7B 01 9F 40 20 50 00 01 12 AA BB CC
        #
        # 06    : Device type
        # 0FA0  : Temperature, (MSB)0000 1111 1010 0000(LSB) = 4000, Scale 200 => 4000/200 = 20.0
        # 55A8  : Humidity, (MSB)0101 0101 1010 1000(LSB) = 21928, Scale 400 => 21928/400 = 54.82
        # C87D  : Pressure, (MSB)1100 1000 0111 1101(LSB) = 51325, Scale 100, Bias 500 => 51325/100 + 500 = 1013.25
        # 007B  : PM25, (MSB)0000 0000 0111 1011(LSB) = 123, Scale 10 => 123/10 = 12.3
        # 019F  : CO2, (MSB)0000 0001 1001 1111(LSB) = 415
        # 40    : VOC, (MSB)0100 0000(LSB) = 64
        # 20    : NOX, (MSB)0010 0000(LSB) = 32
        # 50    : Luminosity, (MSB)0101 0000(LSB) = 80, xlate => 31.8852806787366
        # 00    : Ignored
        # 01    : SeqNo, (MSB)0000 0001(LSB) = 1
        # 12    : Flags, (MSB)0001 0010(LSB) = 18
        #

        self._test_parsing(
            b'\x06\x0F\xA0\x55\xA8\xC8\x7D\x00\x7B\x01\x9F\x40\x20\x50\x00\x01\x12\xAA\xBB\xCC',
            {
                'temperature': {
                    'Temperature': 20.0,
                    'Humidity': 54.82,
                    'Pressure': 1013.25,
                    'PM25': 12.3,
                    'CO2': 415,
                    'VOC': 64,
                    'NOX': 32,
                    'Luminosity': 31.8852806787366,
                }
            }
        )
