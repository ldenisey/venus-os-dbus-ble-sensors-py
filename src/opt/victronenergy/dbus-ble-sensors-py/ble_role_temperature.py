from ble_role import BleRole
from ve_types import *
import logging


class BleRoleTemperature(BleRole):
    """
    Temperature sensor role class.
    Device claiming this role must provide a 'Temperature' item, can provide a 'Humidity' item as well.
    """

    NAME = 'temperature'

    TEMPERATURE_TYPES = {
        0: 'Battery',
        1: 'Fridge',
        2: 'Generic',
        3: 'Room',
        4: 'Outdoor',
        5: 'WaterHeater',
        6: 'Freezer'
    }

    def __init__(self, config: dict = None):
        super().__init__()

        self.info.update(
            {
                'name': 'temperature',
                'dev_instance': 20,
                'settings': [
                    {
                        'name': 'TemperatureType',
                        'props': {
                            'type': VE_SN32,
                            'def': 2,
                            'min': 0,
                            'max': 6
                        }
                    },
                    {
                        'name': 'Offset',
                        'props': {
                            'type': VE_SN32,
                            'def': 0,
                            'min': -100,
                            'max': 100
                        },
                        'onchange': self.offset_update
                    },
                ],
            },
        )
        self._raw_temp = 0

    def update_data(self, role_service, sensor_data: dict):
        # Keeping track of sensor latest value for offset updates
        self._raw_temp = sensor_data.get('Temperature', None)
        if self._raw_temp is None:
            logging.warning(f"{self._plog} Temperature data not found in sensor data")
            return

        # Apply offset to temperature value
        if (offset := role_service['Offset']):
            sensor_data['Temperature'] = self._raw_temp + offset

    def offset_update(self, role_service, new_value):
        role_service['Temperature'] = self._raw_temp + new_value
