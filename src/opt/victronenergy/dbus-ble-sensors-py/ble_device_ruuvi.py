from __future__ import annotations
from ve_types import *
from ble_device import BleDevice
import logging
import math
from dbus_role_service import DbusRoleService


class BleDeviceRuuvi(BleDevice):
    """
    Ruuvi devices class managing :
    - Ruuvi Tag
    - Ruuvi Air

    Cf.
    - https://ruuvi.com/ruuvitag/
    - https://github.com/victronenergy/dbus-ble-sensors/blob/master/src/ruuvi.c
    """

    MANUFACTURER_ID = 0x0499

    @staticmethod
    def _get_low_battery_state(role_service: DbusRoleService) -> int:
        level = 2.5
        if (temperature := role_service['temperature']) is not None:
            if temperature < -20:
                level = 2.0
            elif temperature < 0:
                level = 2.2

        if role_service['/Alarms/LowBattery'] is True:
            level += 0.4

        return int(role_service['BatteryVoltage'] < level)

    @staticmethod
    def _xlate_lum(value: int) -> int:
        scale = 16 * math.log(2) / 254
        value_int8 = value & 0xff
        return math.exp(value_int8 * scale) - 1.0

    MODELS = {
        5: {  # Format 5, aka RAWv2
            'device_name': 'Ruuvi',
            'roles': {'temperature': {}, 'movement': {}},
            'regs': [
                {
                    'name':  'Temperature',
                    'type': VE_SN16,
                    'offset': 1,
                    'scale': 200,
                    'inval': 0x8000,
                    'roles': ['temperature'],
                    'flags': ['REG_FLAG_BIG_ENDIAN', 'REG_FLAG_INVALID'],
                    # .format	= &veUnitCelsius1Dec,
                },
                {
                    'name':  'Humidity',
                    'type': VE_UN16,
                    'offset': 3,
                    'scale': 400,
                    'inval': 0xffff,
                    'roles': ['temperature'],
                    'flags': ['REG_FLAG_BIG_ENDIAN', 'REG_FLAG_INVALID'],
                    # .format	= &veUnitPercentage,
                },
                {
                    'name':  'Pressure',
                    'type': VE_UN16,
                    'offset': 5,
                    'scale': 100,
                    'bias': 500,
                    'inval': 0xffff,
                    'roles': ['temperature'],
                    'flags': ['REG_FLAG_BIG_ENDIAN', 'REG_FLAG_INVALID'],
                    # .format	= &veUnitHectoPascal,
                },
                {
                    'name': 'AccelX',
                    'type': VE_SN16,
                    'offset': 7,
                    'scale': 1000,
                    'inval': 0x8000,
                    'roles': ['movement'],
                    'flags': ['REG_FLAG_BIG_ENDIAN', 'REG_FLAG_INVALID'],
                    # .format	= &veUnitG2Dec,
                },
                {
                    'name': 'AccelY',
                    'type': VE_SN16,
                    'offset': 9,
                    'scale': 1000,
                    'inval': 0x8000,
                    'roles': ['movement'],
                    'flags': ['REG_FLAG_BIG_ENDIAN', 'REG_FLAG_INVALID'],
                    # .format	= &veUnitG2Dec,
                },
                {
                    'name': 'AccelZ',
                    'type': VE_SN16,
                    'offset': 11,
                    'scale': 1000,
                    'inval': 0x8000,
                    'roles': ['movement'],
                    'flags': ['REG_FLAG_BIG_ENDIAN', 'REG_FLAG_INVALID'],
                    # .format	= &veUnitG2Dec,
                },
                {
                    'name': 'BatteryVoltage',
                    'type': VE_UN16,
                    'offset': 13,
                    'shift': 5,
                    'bits': 11,
                    'scale': 1000,
                    'bias': 1.6,
                    'inval': 0x3ff,
                    'flags': ['REG_FLAG_BIG_ENDIAN', 'REG_FLAG_INVALID'],
                    # .format	= &veUnitVolt2Dec,
                },
                {
                    'name': 'TxPower',
                    'type': VE_UN8,
                    'offset': 14,
                    'bits': 5,
                    'scale': 0.5,
                    'bias': -40,
                    'inval': 0x1f,
                    'flags': ['REG_FLAG_INVALID'],
                    # .format	= &veUnitdBm,
                },
                {
                    'name': 'SeqNo',
                    'type': VE_UN16,
                    'offset': 16,
                    'inval': 0xffff,
                    'flags': ['REG_FLAG_BIG_ENDIAN', 'REG_FLAG_INVALID'],
                    # .format	= &veUnitNone,
                },
            ],
            'alarms': [
                {
                    'name': '/Alarms/LowBattery',
                    'update': _get_low_battery_state
                }
            ]
        },
        6: {  # Format 6
            'device_name': 'Ruuvi Air',
            'roles': {'temperature': {}},
            'regs': [
                {
                    'name':  'Temperature',
                    'type': VE_SN16,
                    'offset': 1,
                    'scale': 200,
                    'inval': 0x8000,
                    'flags': ['REG_FLAG_BIG_ENDIAN', 'REG_FLAG_INVALID'],
                    # .format	= &veUnitCelsius1Dec,
                },
                {
                    'name':  'Humidity',
                    'type': VE_UN16,
                    'offset': 3,
                    'scale': 400,
                    'inval': 0xffff,
                    'flags': ['REG_FLAG_BIG_ENDIAN', 'REG_FLAG_INVALID'],
                    # .format	= &veUnitPercentage,
                },
                {
                    'name':  'Pressure',
                    'type': VE_UN16,
                    'offset': 5,
                    'scale': 100,
                    'bias': 500,
                    'inval': 0xffff,
                    'flags': ['REG_FLAG_BIG_ENDIAN', 'REG_FLAG_INVALID'],
                    # .format	= &veUnitHectoPascal,
                },
                {
                    'name':  'PM25',
                    'type': VE_UN16,
                    'offset': 7,
                    'scale': 10,
                    'inval': 0xffff,
                    'flags': ['REG_FLAG_BIG_ENDIAN', 'REG_FLAG_INVALID'],
                    # .format	= &veUnitUgM3,
                },
                {
                    'name':  'CO2',
                    'type': VE_UN16,
                    'offset': 9,
                    'inval': 0xffff,
                    'flags': ['REG_FLAG_BIG_ENDIAN', 'REG_FLAG_INVALID'],
                    # .format	= &veUnitPPM,
                },
                {
                    'name':  'VOC',
                    'type': VE_UN8,
                    'offset': 11,
                    # .format	= &veUnitIndex,
                },
                {
                    'name':  'NOX',
                    'type': VE_UN8,
                    'offset': 12,
                    # .format	= &veUnitIndex,
                },
                {
                    'name':  'Luminosity',
                    'type': VE_UN8,
                    'offset': 13,
                    'xlate': _xlate_lum,
                    'inval': 0xff,
                    'flags': ['REG_FLAG_INVALID'],
                    # .format	= &veUnitLux,
                },
                {
                    'name':  'SeqNo',
                    'type': VE_UN8,
                    'offset': 15,
                    'roles': [None]
                    # .format	= &veUnitLux,
                },
                {
                    'name':  'Flags',
                    'type': VE_UN8,
                    'offset': 16,
                    'roles': [None]
                    # .format	= &veUnitNone,
                },
            ],
        }
    }

    def __init__(self, address: str):
        super().__init__(address)
        self.manufacturer_data_length = None

    def configure(self, manufacturer_data: bytes):
        self.info.update({
            'product_name': 'RuuviTag',
            'product_id': 0xC029,
            'dev_prefix': 'ruuvi',
        })

        model_id = self._load_number(
            {'name':  'model', 'type': VE_UN8, 'offset': 0},
            manufacturer_data
        )
        model_info = BleDeviceRuuvi.MODELS.get(model_id, None)
        if model_info is None:
            raise ValueError(f"Unknown Ruuvi model ID: {model_id}")

        self.manufacturer_data_length = 24 if model_id == 5 else 20
        self.info.update(model_info)

    def check_manufacturer_data(self, manufacturer_data: bytes) -> bool:
        if (length := self.manufacturer_data_length) is None:
            return len(manufacturer_data) >= 1
        else:
            return len(manufacturer_data) == self.manufacturer_data_length

    def update_data(self, role_service: DbusRoleService, sensor_data: dict):
        flags = sensor_data.get('Flags', None)
        if flags is None or flags > 255:
            logging.warning(f"{self._plog} can not update sensor data, missing Flags value")
            return

        voc = sensor_data.get('VOC', None)
        if voc is not None:
            sensor_data['VOC'] = (voc << 1) | ((flags >> 6) & 1)
            if sensor_data['VOC'] == 0x1ff:
                sensor_data['VOC'] = None

        nox = sensor_data.get('NOX', None)
        if nox is not None:
            sensor_data['NOX'] = (nox << 1) | ((flags >> 7) & 1)
            if sensor_data['NOX'] == 0x1ff:
                sensor_data['NOX'] = None
