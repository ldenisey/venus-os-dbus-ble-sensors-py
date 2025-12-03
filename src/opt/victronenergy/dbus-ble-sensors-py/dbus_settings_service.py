from __future__ import annotations
import os
import dbus
import logging
from vedbus import VeDbusItemImport, VeDbusItemExport


class DbusSettingsService(object):
    """
    Inspired from SettingsDevice class of settingsdevice.py file of velib_python, but :
    - removing the use of arbitrary setting names, using paths instead
    - allowing reading settings
    - allowing different callbacks for each settings
    - providing proxy item creation helper methods
    """

    _SETTINGS_SERVICENAME = 'com.victronenergy.settings'

    def __init__(self):
        self._bus: dbus.Bus = None
        self._paths = {}
        if self._bus is None:
            self._bus = dbus.SessionBus() if 'DBUS_SESSION_BUS_ADDRESS' in os.environ else dbus.SystemBus()
        # Check settings service exists
        if self._SETTINGS_SERVICENAME not in self._bus.list_names():
            self._bus = None
            raise Exception(f"Dbus service {self._SETTINGS_SERVICENAME!r} does not exist.")

    def get_item(self, path: str, def_value: object = None, min_value: int = 0, max_value: int = 0) -> VeDbusItemImport:
        # Get the setting item, initializing it only if it does not exists and if a default value is given
        if (item := self._paths.get(path, None)) is None:
            item = VeDbusItemImport(self._bus, self._SETTINGS_SERVICENAME, path)
            if not item.exists and def_value is not None:
                item = self.set_item(path, def_value, min_value, max_value)
            self._paths[path] = item
        return item

    def get_value(self, path) -> object:  # int, float, str, None
        return self.get_item(path).get_value()

    def set_item(self, path: str, def_value: object = None, min_value: int = 0, max_value: int = 0, silent=False, callback=None) -> VeDbusItemImport:
        busitem = VeDbusItemImport(self._bus, self._SETTINGS_SERVICENAME, path, callback)
        if not busitem.exists or (def_value, min_value, max_value, silent) != busitem._proxy.GetAttributes():
            # Get value type
            if isinstance(def_value, (int, dbus.Int64)):
                itemType = 'i'
            elif isinstance(def_value, float):
                itemType = 'f'
            else:
                itemType = 's'

            # Add the setting
            setting_item = VeDbusItemImport(self._bus, self._SETTINGS_SERVICENAME, '/Settings', createsignal=False)
            setting_path = path.replace('/Settings/', '', 1)
            if silent:
                setting_item._proxy.AddSilentSetting('', setting_path, def_value, itemType, min_value, max_value)
            else:
                setting_item._proxy.AddSetting('', setting_path, def_value, itemType, min_value, max_value)

            # Get the setting as a victron bus item
            busitem = VeDbusItemImport(self._bus, self._SETTINGS_SERVICENAME, path, callback)

        self._paths[path] = busitem
        return busitem

    def set_value(self, path, new_value):
        if (setting := self._paths.get(path, None)) is None:
            logging.error(f"Can not set value of non-existing {path!r} to {new_value!r}.")
        else:
            if (result := setting.set_value(new_value)) != 0:
                logging.error(f"Failed to set setting {path!r} to {new_value!r}, result={result}.")

    def set_proxy_callback(self, setting_path: str, remote_item: VeDbusItemExport):
        def _callback(service_name, change_path, changes):
            if service_name != DbusSettingsService._SETTINGS_SERVICENAME or change_path != setting_path:
                return
            new_value = changes['Value']
            if new_value != remote_item.local_get_value():
                remote_item.local_set_value(new_value)
        self.get_item(setting_path).eventCallback = _callback

    def unset_proxy_callback(self, setting_path: str):
        self.get_item(setting_path).eventCallback = None

    def __getitem__(self, path):
        return self.get_value(path)

    def __setitem__(self, path, new_value):
        self.set_value(path, new_value)
