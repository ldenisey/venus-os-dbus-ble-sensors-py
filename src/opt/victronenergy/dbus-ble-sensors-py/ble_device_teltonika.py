from ve_types import *
import logging
from ble_device import BleDevice
from dbus_role_service import DbusRoleService


class BleDeviceTeltonika(BleDevice):
    """
    Teltonika device class managing EYE Sensor (BTSMP1) devices.

    Advertising data format depends on the configuration of the device, resulting roles are adapted accordingly
    during initialization. In case of device configuration change, you must restart the service or your Venus OS device.

    Protocol specifications:
        - https://wiki.teltonika-gps.com/view/EYE_SENSOR_/_BTSMP1#Sensor_advertising
    Data parsing example:
        - https://wiki.teltonika-gps.com/view/EYE_SENSOR_/_BTSMP1#EYE_Sensor_Bluetooth%C2%AE_frame_parsing_example
    """

    MANUFACTURER_ID = 0x089A # 'Private limited company "Teltonika"'

    def configure(self, manufacturer_data: bytes):
        self.info.update({
            'manufacturer_id': BleDeviceTeltonika.MANUFACTURER_ID,
            'product_id': 0x3042,
            'product_name': 'Teltonika sensor',
            'device_name': 'Teltonika Eye',
            'dev_prefix': 'teltonika',
            'alarms': [
                {
                    'name': '/Alarms/LowBattery',
                    'update': self._get_low_battery_state
                }
            ]
        })
        self._compute_regs(manufacturer_data)
        logging.debug(f"{self._plog} computed regs: {self.info['regs']!r}")

    def _compute_regs(self, manufacturer_data: bytes):
        self.info['regs'] = [
            {
                'name': 'Version',
                'type': VE_UN8,
                'offset': 0,
                'roles': [None],
            },
            {
                'name': 'EyeFlags',
                'type': VE_UN8,
                'offset': 1,
                'roles': [None],
            },
            {
                'name': 'LowBattery',
                'type': VE_UN8,
                'offset': 1,
                'shift': 6,
                'bits': 1,
            }
        ]

        # Compute regs from flags
        offset = 2
        flag_mag = self._load_number({
            'name': 'FlagMag',
            'type': VE_UN8,
            'offset': 1,
            'shift': 2,
            'bits': 1,
        }, manufacturer_data)
        if flag_mag:
            self.info['regs'].append({
                'name': 'InputState',  # Magnet presence
                'type': VE_UN8,
                'offset': 1,
                'shift': 3,
                'bits': 1,
                'roles': ['digitalinput'],
            })
            self.info['roles']['digitalinput'] = {}

        flag_temp = self._load_number({
            'name': 'FlagTemp',
            'type': VE_UN8,
            'offset': 1,
            'shift': 0,
            'bits': 1,
        }, manufacturer_data)
        if flag_temp:
            self.info['regs'].append({
                'name': 'Temperature',
                'type': VE_SN16,
                'offset': offset,
                'scale': 100,
                'flags': ['REG_FLAG_BIG_ENDIAN'],
                'roles': ['temperature'],
            })
            self.info['roles']['temperature'] = {}
            offset = offset + 2

        flag_humid = self._load_number({
            'name': 'FlagHumid',
            'type': VE_UN8,
            'offset': 1,
            'shift': 1,
            'bits': 1,
        }, manufacturer_data)
        if flag_humid:
            self.info['regs'].append({
                'name': 'Humidity',
                'type': VE_UN8,
                'offset': offset,
                'flags': ['REG_FLAG_BIG_ENDIAN'],
                'roles': ['temperature'],
            })
            self.info['roles']['temperature'] = {}
            offset = offset + 1

        flag_mov = self._load_number({
            'name': 'FlagMov',
            'type': VE_UN8,
            'offset': 1,
            'shift': 4,
            'bits': 1,
        }, manufacturer_data)
        if flag_mov:
            self.info['regs'].append({
                'name': 'MovementState',
                'type': VE_UN8,
                'offset': offset,
                'shift': 7,
                'bits': 1,
                'roles': ['movement'],
            })
            self.info['regs'].append({
                'name': 'MovementCount',
                'type': VE_UN16,
                'offset': offset,
                'bits': 15,
                'flags': ['REG_FLAG_BIG_ENDIAN'],
                'roles': ['movement'],
            })
            self.info['roles']['movement'] = {}
            offset = offset + 2

        flag_angle = self._load_number({
            'name': 'FlagAngle',
            'type': VE_UN8,
            'offset': 1,
            'shift': 5,
            'bits': 1,
        }, manufacturer_data)
        if flag_angle:
            self.info['regs'].append({
                'name': 'Pitch',
                'type': VE_SN8,
                'offset': offset,
                'roles': ['movement'],
            })
            offset = offset + 1
            self.info['regs'].append({
                'name': 'Roll',
                'type': VE_SN16,
                'offset': offset,
                'flags': ['REG_FLAG_BIG_ENDIAN'],
                'roles': ['movement'],
            })
            self.info['roles']['movement'] = {}
            offset = offset + 2

        flag_bat = self._load_number({
            'name': 'FlagBat',
            'type': VE_UN8,
            'offset': 1,
            'shift': 7,
            'bits': 1,
        }, manufacturer_data)
        if flag_bat:
            self.info['regs'].append({
                'name': 'BatteryVoltage',
                'type': VE_UN8,
                'offset': offset,
                'scale': 1/10,
                'bias': 2000,
            })

    def _get_low_battery_state(self, role_service: DbusRoleService) -> int:
        return int((role_service['LowBattery'] or 0) >= 1)
