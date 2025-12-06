import os
import logging
import dbus
from dbus_settings_service import DbusSettingsService
from ble_role import BleRole
from functools import partial
from conf import PROCESS_NAME, PROCESS_VERSION
from vedbus import VeDbusService, VeDbusItemImport, VeDbusItemExport


class DbusRoleService(object):
    """
    Role service class. Responsible for holding and sharing data through a dedicated dbus service.
    """

    def __init__(self, ble_device, ble_role: BleRole):
        # private=True to allow creation of multiple services in the same app
        self._bus: dbus.Bus = dbus.SessionBus(
            private=True) if 'DBUS_SESSION_BUS_ADDRESS' in os.environ else dbus.SystemBus(private=True)
        self._dbus_settings = DbusSettingsService()
        self._ble_device = ble_device
        self.ble_role = ble_role
        self._dbus_service: VeDbusService = None
        self._service_name: str = None
        self._dbus_iface = dbus.Interface(
            self._bus.get_object('org.freedesktop.DBus', '/org/freedesktop/DBus'),
            'org.freedesktop.DBus')
        self._dev_id = self._ble_device.info['dev_id']
        self._dbus_id = f"{self._dev_id}/{self.ble_role.NAME}"
        self._init_dbus_service()

    def is_connected(self) -> bool:
        # Local check
        if self._dbus_service is None:
            return False

        # Dbus check
        return self._dbus_iface.NameHasOwner(self._service_name)

    def _get_vrm_instance(self) -> int:
        # Try and get instance saved in settings
        if (dev_instance := self._dbus_settings.get_value(f"/Settings/Devices/{self._dbus_id}/VrmInstance")):
            logging.info(f"{self._ble_device._plog} vrm instance {dev_instance!r} found for device {self._dbus_id!r}")
            return dev_instance

        # Load devices from settings
        devices_string: dict = self._dbus_settings.get_item('/Settings/Devices').get_value()
        if not devices_string:
            return -1

        # Filter existing ClassAndVrmInstance and get VrmInstance
        role_name = self.ble_role.NAME
        existing_instances = set()
        for key, value in devices_string.items():
            if '/ClassAndVrmInstance' in key and value.startswith(role_name):
                existing_instances.add(int(value[len(role_name) + 1:]))
            elif f"{role_name}/VrmInstance" in key:
                existing_instances.add(int(value))

        # Increment instance until free one found
        cur_instance = int(self.ble_role.info['dev_instance'])
        while cur_instance in existing_instances:
            cur_instance += 1

        # Save instance in settings
        logging.info(f"{self._ble_device._plog} assigning vrm instance {cur_instance!r} for role {role_name!r}")
        self._dbus_settings.set_item(f"/Settings/Devices/{self._dbus_id}/VrmInstance", cur_instance)
        return cur_instance

    def _init_dbus_service(self):
        self._service_name = f"com.victronenergy.{self.ble_role.NAME}.{self._dev_id}"

        logging.debug(f"{self._ble_device._plog} initializing dbus {self._service_name!r}")
        self._dbus_service = VeDbusService(self._service_name, self._bus, False)

        # Add mandatory data
        self._dbus_service.add_path('/Mgmt/ProcessName', PROCESS_NAME)
        self._dbus_service.add_path('/Mgmt/ProcessVersion', PROCESS_VERSION)
        self._dbus_service.add_path('/Mgmt/Connection', "Bluetooth LE")
        # Device instance will be set at connection to avoid conflicts
        self._dbus_service.add_path('/ProductId', self._ble_device.info['product_id'])
        self._dbus_service.add_path('/ProductName', self._ble_device.info['product_name'])
        self._dbus_service.add_path('/FirmwareVersion', self._ble_device.info['firmware_version'])
        self._dbus_service.add_path('/HardwareVersion', self._ble_device.info['hardware_version'])
        self._dbus_service.add_path('/Connected', 1, writeable=True)
        self._dbus_service.add_path('/Status', 0, writeable=True)
        self._dbus_service.add_path('/DeviceName', self._ble_device.info['device_name'])

    def _add_settings(self, settings: dict):
        for setting in settings:
            callback = None
            if (onchange := setting.get('onchange', None)) is not None:
                callback = partial(onchange, self)
            self.add_setting(setting, callback)

    def load_settings(self):
        self._init_custom_name()

        self._add_settings(self.ble_role.info['settings'])
        for alarm in self.ble_role.info['alarms']:
            self.add_alarm(alarm)

        self.ble_role.init(self)

        self._add_settings(self._ble_device.info['settings'])
        for alarm in self._ble_device.info['alarms']:
            self.add_alarm(alarm)

    def connect(self):
        if not self.is_connected():
            # Device instance check
            if not self._get_value('/DeviceInstance'):
                self._set_value('/DeviceInstance', self._get_vrm_instance())

            logging.info(f"{self._ble_device._plog} registering {self._service_name!r} dbus service on bus {self._bus}")
            self._dbus_service.register()

    def disconnect(self):
        if not self.is_connected():
            return
        logging.info(f"{self._ble_device._plog} releasing '{self._service_name}' dbus service")
        self._dbus_service._dbusname.__del__()
        self._dbus_service._dbusname = None

    def on_enabled_changed(self, is_enabled: int):
        if is_enabled:
            self.connect()
        else:
            self.disconnect()

    @staticmethod
    def _clear_path(path: str) -> str:
        return f"/{path.lstrip('/').rstrip('/')}"

    def _get_item(self, path: str) -> VeDbusItemExport:
        return self._dbus_service._dbusobjects.get(self._clear_path(path), None)

    def _get_value(self, path: str) -> object:  # int, float, str, None
        if (item := self._get_item(path)):
            return item.local_get_value()
        return None

    def _set_value(self, path: str, value: object):
        clean_path = self._clear_path(path)
        with self._dbus_service as service:
            if clean_path not in service:
                logging.debug(
                    f"{self._ble_device._plog} creating item {self._service_name!r}@{clean_path!r} to {value!r}")
                service.add_path(clean_path, value, writeable=True)
            elif service[clean_path] != value:
                logging.debug(
                    f"{self._ble_device._plog} updating item {self._service_name!r}@{clean_path!r} to {value!r}")
                service[clean_path] = value

    def _delete_item(self, path: str):
        clean_path = self._clear_path(path)
        if self._dbus_service._dbusobjects.get(clean_path, None) is None:
            logging.error(f"Can not delete non-existing {clean_path!r}")
        else:
            logging.debug(f"Deleting item {self._service_name!r}@{clean_path!r}")
            with self._dbus_service as service:
                del service[clean_path]

    def __getitem__(self, path: str) -> object:  # int, float, str, None
        return self._get_value(path)

    def __setitem__(self, path: str, new_value: object):
        self._set_value(path, new_value)

    def __delitem__(self, path: str):
        self._delete_item(path)

    def _set_proxy_callback(self, item_path: str, setting_item: VeDbusItemImport, callback=None):
        def _callback(change_path, new_value):
            if change_path != item_path:
                return 0
            if new_value != setting_item.get_value():
                setting_item.set_value(new_value)
            if callback:
                callback(new_value)
            return 1
        self._dbus_service._dbusobjects[item_path]._onchangecallback = _callback

    def _set_proxy_setting(self, setting_path: str, item_path: str, default_value: object, min_value: int = 0, max_value: int = 0, callback=None):
        logging.debug(
            f"Creating setting {setting_path!r} proxy to {item_path!r} with: {default_value!r} {min_value!r} {max_value!r} {callback!r}")
        # Get or set setting
        setting_item = self._dbus_settings.get_item(setting_path, default_value, min_value, max_value)

        # Init item and custom callback
        self._set_value(item_path, setting_item.get_value())
        self._set_proxy_callback(item_path, setting_item, callback)

        # Set settings callback
        setting_item = self._dbus_settings.set_proxy_callback(setting_path, self._get_item(item_path))

    def get_dev_id(self) -> str:
        return self._dev_id

    def get_dbus_id(self) -> str:
        return self._dbus_id

    def _init_custom_name(self):
        self._set_proxy_setting(
            f"/Settings/Devices/{self._dbus_id}/CustomName",
            '/CustomName',
            '',
        )

    def get_custom_name(self) -> str:
        return self._get_value('/CustomName')

    def get_device_name(self) -> str:
        return self._get_value('/DeviceName')

    def add_setting(self, setting: dict, callback=None):
        name = self._clear_path(setting['name'])
        props = setting['props']
        self._set_proxy_setting(
            f"/Settings/Devices/{self._dbus_id}{name}",
            name,
            props['def'],
            props['min'],
            props['max'],
            callback=callback
        )

    def add_alarm(self, alarm: dict):
        self._set_value(alarm['name'], 0)

    def update_alarm(self, alarm: dict):
        alarm_state = alarm['update'](self)
        self._set_value(alarm['name'], alarm_state)
