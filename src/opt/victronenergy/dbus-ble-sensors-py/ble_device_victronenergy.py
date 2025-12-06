from ve_types import *
from ble_device import BleDevice
from dbus_role_service import DbusRoleService


class BleDeviceVictronEnergy(BleDevice):
    """
    Victron Energy devices class managing SolarSense 750 BLE devices.

    Cf.
    - https://github.com/victronenergy/dbus-ble-sensors/blob/master/src/solarsense.c
    - https://github.com/victronenergy/gui-v2/blob/main/data/mock/conf/services/meteo-solarsense.json
    """

    MANUFACTURER_ID = 0x02E1

    def configure(self, _: bytes):
        self.info.update({
            'manufacturer_id': BleDeviceVictronEnergy.MANUFACTURER_ID,
            'product_id': 0xC050,
            'product_name': 'SolarSense sensor',
            'device_name': 'SolarSense',
            'dev_prefix': 'solarsense',
            'roles': {'meteo': {}},
            'regs': [
                {
                    'name': 'ErrorCode',
                    'type': VE_UN32,
                    'offset': 8,
                },
                {
                    'name': 'ChrErrorCode',
                    'type': VE_UN8,
                    'offset': 12,
                    'flags': ['REG_FLAG_INVALID'],
                    'inval': 0xFF
                },
                {
                    'name':  'InstallationPower',
                    'type': VE_UN32,
                    'offset': 13,
                    'scale': 1,
                    'bits': 20,
                    'flags': ['REG_FLAG_INVALID'],
                    'inval': 0xfffff,
                    # .format = &veUnitWatt,
                },
                {
                    'name':  'TodaysYield',
                    'type': VE_UN32,
                    'offset': 15,
                    'shift': 4,
                    'scale': 100,
                    'bits': 20,
                    'flags': ['REG_FLAG_INVALID'],
                    'inval': 0xfffff,
                    # .format = &veUnitKiloWattHour,
                },
                {
                    'name':  'Irradiance',
                    'type': VE_UN16,
                    'offset': 18,
                    'scale': 10,
                    'bits': 14,
                    'flags': ['REG_FLAG_INVALID'],
                    'inval': 0x3fff,
                    # .format = &veUnitIrradiance1Dec,
                },
                {
                    'name':  'CellTemperature',
                    'type': VE_UN16,
                    'offset': 19,
                    'bits': 11,
                    'shift': 6,
                    'scale': 10,
                    'bias': -60,
                    'flags': ['REG_FLAG_INVALID'],
                    'inval': 0x7ff,
                    # .format	= &veUnitCelsius1Dec,
                },
                {
                    'name':  'UnspecifiedRemnant',
                    'type': VE_UN8,
                    'offset': 20,
                    'bits': 1,
                    'shift': 1,
                },
                {
                    'name':  'BatteryVoltage',
                    'type': VE_UN16,
                    'offset': 21,
                    'bits': 8,
                    'shift': 2,
                    'scale': 100,
                    'bias': 1.7,
                    'flags': ['REG_FLAG_INVALID'],
                    'inval': 0xff,
                    # .format = &veUnitVolt2Dec,
                },
                {
                    'name':  'TxPowerLevel',
                    'type': VE_UN8,
                    'offset': 22,
                    'bits': 1,
                    'shift': 2,
                    'flags': ['REG_FLAG_INVALID'],
                    'inval': 0xff,
                    'xlate': self.xlate_txpower,
                    # .format	= &veUnitdBm,
                },
                {
                    'name':  'TimeSinceLastSun',
                    'type': VE_UN16,
                    'offset': 22,
                    'bits': 7,
                    'shift': 3,
                    'flags': ['REG_FLAG_INVALID'],
                    'inval': 0x7f,
                    'xlate': self.xlate_tss,
                    # .format	= &veUnitMinutes,
                },
            ],
            'alarms': [
                {
                    'name': '/Alarms/LowBattery',
                    'update': self._get_low_battery_state
                }
            ]
        })

    def check_manufacturer_data(self, manufacturer_data: bytes) -> bool:
        if len(manufacturer_data) < 22 or manufacturer_data[0] != 0x10 or manufacturer_data[4] != 0xff or manufacturer_data[7] != 0x01:
            return False
        return True

    def xlate_txpower(self, value: object) -> int:
        return 6 if value else 0

    def xlate_tss(self, value: int) -> int:
        if value <= 29:
            return value * 2
        elif value <= 95:
            return 60 + 10 * (value - 30)
        elif value <= 126:
            return 720 + 30 * (value - 96)
        return value

    def _get_low_battery_state(self, role_service: DbusRoleService) -> int:
        level = 3.6 if role_service['/Alarms/LowBattery'] is True else 3.2
        return int(role_service['BatteryVoltage'] < level)
