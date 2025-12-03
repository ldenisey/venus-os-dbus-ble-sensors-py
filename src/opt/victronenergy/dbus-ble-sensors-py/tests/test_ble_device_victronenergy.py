import sys
import os
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..', 'ext'))
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..', 'ext', 'velib_python'))
from ble_device_victronenergy import BleDeviceVictronEnergy
from ble_device_base_tests import BleDeviceBaseTests


class BleDeviceVictronEnergyTests(BleDeviceBaseTests):
    # To be executed with command : python3 -m unittest test_ble_device_victronenergy.py

    def setUp(self):
        super().setUp(BleDeviceVictronEnergy, '012345678901', 'SolarSense_750')

    def test_1(self):
        """
        Test based mocked data:
        - https://github.com/victronenergy/gui-v2/blob/main/data/mock/conf/services/meteo-solarsense.json
        """
        # 10000000FF000001 : unused and predefined bytes
        # 05140000: ErrorCode, 1405 = 5125
        # 74      : ChrErrorCode, 74 = 116
        # 000000  : InstallationPower, 0
        # 000000  : TodaysYield, 0
        # 00C0    : Irradiance C000=(MSB)1100 0000 0000 0000(LSB), Bits 14 => (MSB)00 0000 0000 0000(LSB) = 0
        # C0C248  : CellTemperature,    48C2C0  => (MSB)0100 1000 1100 0010 1100 0000(LSB)
        #                               Shift 6 => (MSB)01 0010 0011 0000 1011(LSB)
        #                               Bits 11 => (MSB)011 0000 1011(LSB) = 779
        #                               Scale 10, bias -60 => 779/10 - 60 = 17,9
        # C2      : UnspecifiedRemnant, C2=(MSB)1100 0010(LSB), Shift 1 => C2=(MSB)110 0001(LSB), Bits 1=1
        # 4877    : BatteryVoltage,     7748=(MSB)0111 0111 0100 1000(LSB)
        #                               Shift 2 => (MSB)01 1101 1101 0010(LSB)
        #                               Bits 8 => (MSB)1101 0010(LSB)
        #                               Scale 100, bias 1.7 => 210/100 + 1.7 = 3.8
        # 77      : TxPowerLevel, 77=(MSB)0111 0111(LSB), Shift 2 => (MSB)01 1101(LSB), Bits 1 => 1, Xlate => 6
        # 7701    : TimeSinceLastSun,   0177=(MSB)0000 0001 0111 0111(LSB)
        #                               Shift 3 => (MSB)0 0000 0010 1110(LSB)
        #                               Bits 7 => (MSB)010 1110(LSB) = 46
        #                               Xlate => 60 + 10 * (46 - 30) = 220
        self._test_parsing(
            b'\x10\x00\x00\x00\xFF\x00\x00\x01\x05\x14\x00\x00\x74\x00\x00\x00\x00\x00\x00\xC0\xC2\x48\x77\x01',
            {
                'meteo': {
                    'BatteryVoltage': 3.8,
                    'CellTemperature': 17.900000000000006,
                    'ChrErrorCode': 116,
                    'ErrorCode': 5125,
                    'Irradiance': 0,
                    'InstallationPower': 0,
                    'TimeSinceLastSun': 220,
                    'TodaysYield': 0.0,
                    'TxPowerLevel': 6,
                    'UnspecifiedRemnant': 1,
                }
            }
        )
