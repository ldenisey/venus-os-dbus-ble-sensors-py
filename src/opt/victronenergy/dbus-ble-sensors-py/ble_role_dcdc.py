from ble_role import BleRole
from ve_types import *


class BleRoleDcdc(BleRole):
    """
    DC-DC converter role (Venus OS ``dcdc`` service type).

    Used for Victron Orion-TR Smart devices so they appear under the standard
    DC-DC device page in gui-v2 (``PageDcDcConverter.qml``).
    """

    NAME = "dcdc"

    def __init__(self, config: dict = None):
        super().__init__()
        self.info.update(
            {
                "name": "dcdc",
                "dev_instance": 130,
                "settings": [],
                "alarms": [],
            }
        )

    def init(self, role_service):
        svc = role_service._dbus_service
        with svc as s:
            s.add_path("/Dc/In/V", None)
            s.add_path("/Dc/In/I", None)
            s.add_path("/Dc/In/P", None)
            s.add_path("/Dc/0/Voltage", None)
            s.add_path("/Dc/0/Current", None)
            s.add_path("/Dc/0/Power", None)
            s.add_path("/Dc/0/Temperature", None)
            s.add_path("/State", 0)
            s.add_path("/ErrorCode", 0)
            s.add_path("/DeviceOffReason", 0)

            def on_mode(path, value):
                return role_service._ble_device._orion_on_mode_write(role_service, int(value))

            s.add_path("/Mode", 1, writeable=True, onchangecallback=on_mode)
