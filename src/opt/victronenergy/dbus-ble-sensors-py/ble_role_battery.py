from ble_role import BleRole


class BleRoleBattery(BleRole):
    """
    Battery voltage monitor role class.
    Device claiming this role must provide a '/Dc/0/Voltage' item.
    """

    NAME = 'battery'

    def __init__(self, config: dict = None):
        super().__init__()

        self.info.update(
            {
                'name': 'battery',
                'dev_instance': 50,
                'settings': [],
            },
        )
