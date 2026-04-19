"""
Alternator role (Venus OS ``alternator`` service type).

Used for Victron Orion-TR Smart devices when they are operating in a
charger algorithm (bulk / absorption / float / storage).  In that state
the stock ``dbus-victron-orion-tr`` service publishes under
``com.victronenergy.alternator.*`` so the unit appears on the
*DC Sources* page (see ``gui-v2/pages/settings/devicelist/dc-in/
PageAlternator.qml``).  This mirrors that behaviour inside
``dbus-ble-sensors-py``.

When the device is off or running in fixed-output (PSU) mode it should
instead be published as a ``dcdc`` service — see :mod:`ble_role_dcdc`.
The parent device (``BleDeviceOrionTR``) is responsible for swapping
the role in response to state changes.
"""
from ble_role import BleRole


class BleRoleAlternator(BleRole):
    NAME = "alternator"

    def __init__(self, config: dict = None):
        super().__init__()
        self.info.update(
            {
                "name": "alternator",
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
