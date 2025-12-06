from ve_types import *
from ble_device import BleDevice
import logging
from dbus_role_service import DbusRoleService


class BleDeviceMopeka(BleDevice):
    """
    Mopeka devices class managing :
    - Mopeka Pro Check Universal
    - Mopeka Pro Check H2O
    - Mopeka Pro Check LPG
    - Mopeka Pro 200
    - Mopeka Pro Plus
    - Mopeka TD40
    - Mopeka TD200

    Cf.
    - https://mopeka.com/consumer-solutions/
    - https://github.com/victronenergy/dbus-ble-sensors/blob/master/src/mopeka.c
    """

    MANUFACTURER_ID = 0x0059

    MODELS = {
        3: {
            'device_name': 'Mopeka LPG',
            'settings': [
                {
                    'name': "ButaneRatio",
                    'props': {
                        'type': VE_SN32,
                        'def': 0,
                        'min': 0,
                        'max': 100,
                    },
                }
            ],
            'roles': {'tank': {}, 'temperature': {}, 'movement': {}}
        },
        4: {
            'device_name': 'Mopeka Pro200',
            'roles': {'tank': {'flags': ['TANK_FLAG_TOPDOWN']}, 'temperature': {}, 'movement': {}}
        },
        5: {
            'device_name': 'Mopeka H20',
            'roles': {'tank': {}, 'temperature': {}, 'movement': {}}
        },
        8: {
            'device_name': 'Mopeka PPB',
            'settings': [
                {
                    'name': "ButaneRatio",
                    'props': {
                        'type': VE_SN32,
                        'def': 0,
                        'min': 0,
                        'max': 100,
                    },
                }
            ],
            'roles': {'tank': {}, 'temperature': {}, 'movement': {}}
        },
        9: {
            'device_name': 'Mopeka PPC',
            'settings': [
                {
                    'name': "ButaneRatio",
                    'props': {
                        'type': VE_SN32,
                        'def': 0,
                        'min': 0,
                        'max': 100,
                    },
                }
            ],
            'roles': {'tank': {}, 'temperature': {}, 'movement': {}}
        },
        10: {
            'device_name': 'Mopeka TDB',
            'roles': {'tank': {'flags': ['TANK_FLAG_TOPDOWN']}, 'temperature': {}, 'movement': {}}
        },
        11: {
            'device_name': 'Mopeka TDC',
            'roles': {'tank': {'flags': ['TANK_FLAG_TOPDOWN']}, 'temperature': {}, 'movement': {}}
        },
        12: {
            'device_name': 'Mopeka Univ',
            'settings': [
                {
                    'name': "ButaneRatio",
                    'props': {
                        'type': VE_SN32,
                        'def': 0,
                        'min': 0,
                        'max': 100,
                    },
                }
            ],
            'roles': {'tank': {}, 'temperature': {}, 'movement': {}}
        }
    }

    _COEFS_H2O: tuple[float, float, float] = (0.600592, 0.003124, -0.00001368)
    _COEFS_LPG: tuple[float, float, float] = (0.573045, -0.002822, -0.00000535)
    _COEFS_GASOLINE: tuple[float, float, float] = (0.7373417462, -0.001978229885, 0.00000202162)
    _COEFS_AIR: tuple[float, float, float] = (0.153096, 0.000327, -0.000000294)
    _COEFS_BUTANE: tuple[float, float] = (0.03615, 0.000815)

    def _get_model_info(self, manufacturer_data: bytes) -> dict:
        model_id = self._load_number(
            {'name':  'HardwareID', 'type': VE_UN8, 'offset': 0, 'bits': 7, },
            manufacturer_data
        )
        model_info = BleDeviceMopeka.MODELS.get(model_id, None)
        if model_info is None:
            raise ValueError(f"Unknown Mopeka model ID: {model_id}")
        return model_info

    def configure(self, manufacturer_data: bytes):
        model_info = self._get_model_info(manufacturer_data)

        self.info.update({
            'manufacturer_id': BleDeviceMopeka.MANUFACTURER_ID,
            'product_id': 0xC02A,
            'product_name': 'Mopeka sensor',
            'dev_prefix': 'mopeka',
            'regs': [
                {
                    'name':  'HardwareID',
                    'type': VE_UN8,
                    'offset': 0,
                    'bits': 7,
                    # .format	= &veUnitNone,
                },
                {
                    'name':  'TankLevelExtension',
                    'type': VE_UN8,
                    'offset': 0,
                    'shift': 7,
                    'bits': 1,
                    'roles': ['tank'],
                    # .format	= &veUnitNone,
                },
                {
                    'name':  'BatteryVoltage',
                    'type': VE_UN8,
                    'offset': 1,
                    'bits': 7,
                    'scale': 32,
                    # .format	= &veUnitVolt2Dec,
                },
                {
                    'name':  'Temperature',
                    'type': VE_UN8,
                    'offset': 2,
                    'bits': 7,
                    'scale': 1,
                    'bias': -40,
                    'roles': ['temperature'],
                    # .format	= &veUnitCelsius1Dec,
                },
                {
                    'name':  'SyncButton',
                    'type': VE_UN8,
                    'offset': 2,
                    'shift': 7,
                    'bits': 1,
                    'roles': [None],
                    # .format	= &veUnitNone,
                },
                {
                    'name':  'RawValue',
                    'type': VE_UN16,
                    'offset': 3,
                    'bits': 14,
                    'roles': ['tank'],
                    # .format	= &veUnitcm,
                },
                {
                    'name':  'Quality',
                    'type': VE_UN8,
                    'offset': 4,
                    'shift': 6,
                    'bits': 2,
                    'roles': [None],
                    # .format	= &veUnitNone,
                },
                {
                    'name':  'AccelX',
                    'type': VE_SN8,
                    'offset': 8,
                    'scale': 1024,
                    'roles': ['movement'],
                    # .format	= &veUnitG2Dec,
                },
                {
                    'name':  'AccelY',
                    'type': VE_SN8,
                    'offset': 9,
                    'scale': 1024,
                    'roles': ['movement'],
                    # .format	= &veUnitG2Dec,
                }
            ],
            'alarms': [
                {
                    'name': '/Alarms/LowBattery',
                    'update': self._get_low_battery_state
                }
            ]
        })
        self.info.update(model_info)

    def check_manufacturer_data(self, manufacturer_data: bytes) -> bool:
        if len(manufacturer_data) != 10:
            return False

        # Check NIC (Network Interface Controller)
        dev_mac = self.info['dev_mac']
        if manufacturer_data[5] != int(dev_mac[6:8], 16) or \
                manufacturer_data[6] != int(dev_mac[8:10], 16) or \
                manufacturer_data[7] != int(dev_mac[10:], 16):
            return False
        return True

    def _get_scale_butane(self, butane_ratio: int, temperature: float) -> float:
        """
        Calculate the butane scale factor based on temperature and user-defined ratio.
        """
        return self._COEFS_BUTANE[0] + self._COEFS_BUTANE[1] * temperature * (butane_ratio / 100.0)

    def update_data(self, role_service: DbusRoleService, sensor_data: dict):
        """
        Check for presence of extension bit on certain hardware/firmware saturates at 16383.
        When extension bit is set, the raw_value resolution changes to 4us with 16384 us offset.
        Thus old sensors and firmware still have 0 to 16383 us range with 1us, and new
        versions add the range 16384 us to 81916 us with 4 us resolution.
        """
        if (temperature := sensor_data.get('Temperature', None)) is None:
            logging.warning(f"{self._plog} can not update sensor data, missing temperature value")
            return
        temperature += 40

        if (raw_value := sensor_data.get('RawValue', None)) is None:
            logging.warning(f"{self._plog} can not update sensor data, missing raw value")
            return
        if (tank_level_ext := sensor_data.get('TankLevelExtension', None)) is None:
            logging.warning(f"{self._plog} can not update sensor data, missing tank level extension value")
            return
        if tank_level_ext:
            raw_value = 16384 + 4 * raw_value

        if (hardware_id := sensor_data.get('HardwareID', None)) is None:
            logging.warning(f"{self._plog} can not update sensor data, missing hardware ID value")
            return
        coefs = None
        scale = 0.0
        match hardware_id:
            case 3:
                coefs = self._COEFS_LPG
                scale = self._get_scale_butane(role_service['ButaneRatio'], temperature)
            case 4:
                coefs = self._COEFS_AIR
            case 5:
                coefs = self._COEFS_H2O
            case 10:
                coefs = self._COEFS_AIR
            case 11:
                coefs = self._COEFS_AIR
            case 8 | 9 | 12:
                match (fluid_type := role_service['FluidType']):
                    case 1 | 2 | 3 | 5 | 11:
                        coefs = self._COEFS_H2O
                    case 8:
                        coefs = self._COEFS_LPG
                        scale = self._get_scale_butane(role_service['ButaneRatio'], temperature)
                    case 6 | 7:
                        coefs = self._COEFS_GASOLINE
                    case _:
                        logging.warning(f"{self._plog} can not update sensor data, unmanaged fluid type: {fluid_type}")
                        return
            case _:
                logging.warning(f"{self._plog} can not update sensor data, unknown hardware ID: {hardware_id}")
                return
        scale += coefs[0] + coefs[1] * temperature + coefs[2] * temperature * temperature
        sensor_data['RawValue'] = (raw_value * scale) / 10

    def _get_low_battery_state(self, role_service: DbusRoleService) -> int:
        # Percentage based on 3 volt CR2032 battery
        if (battery_voltage := role_service.get('BatteryVoltage', None)) is None:
            return 0
        battery_percentage = max(0, min(100, ((battery_voltage - 2.2) / 0.65) * 100))
        return int(battery_percentage < 15)
