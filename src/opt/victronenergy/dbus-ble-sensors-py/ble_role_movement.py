from ble_role import BleRole
from ve_types import *


class BleRoleMovement(BleRole):
    """
    Movement sensor role class.

    Default alarm is 'Movement', which indicates if movement has been detected. It requires either a 
    'MovementState' or a 'MovementCount' item from the device.

    This role is not officially supported by Venus OS, hence there is no UI nor alarm support for it.
    Primarily created to properly store sensor data and allow custom development.
    """

    NAME = 'movement'

    def __init__(self, config: dict = None):
        super().__init__()
        self._count = None

        self.info.update(
            {
                'name': 'movement',
                'dev_instance': 1,
                'settings': [
                    {
                        'name': 'Alarms/Movement/Enable',
                        'props': {
                            'type': VE_UN8,
                            'def': 0,
                            'min': 0,
                            'max': 1
                        }
                    }
                ],
                'alarms': [
                    {
                        'name': 'Alarms/Movement/State',
                        'update': self.get_alarm_movement
                    }
                ]
            }
        )

    def get_alarm_movement(self, role_service) -> int:
        if role_service['Alarms/Movement/Enable'] is False:
            return 0
        if (movement_state := role_service['MovementState']) is not None:
            return movement_state
        if self._count is not None:
            return int(self._count != role_service['MovementCount'])
        return 0

    def update_data(self, role_service, sensor_data: dict):
        # Keep track of movement count for alarm comparison
        if (count := sensor_data.get('MovementCount', None)) is not None:
            self._count = count
