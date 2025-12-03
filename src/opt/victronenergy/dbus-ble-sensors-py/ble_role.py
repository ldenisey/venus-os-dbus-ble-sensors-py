import os
import inspect
import logging
import importlib.util


class BleRole(object):
    """
    Base class representing a type/kind/class of device.
    Defines common settings and alarms, most of which interacting with Venus OS UI and services through dbus.
    """

    NAME = None  # To be overloaded in children classes: str, role name

    ROLE_CLASSES = {}

    def __init__(self, config: dict = None):
        self.info = {
            'dev_instance': 0,  # Mandatory, int, base dev instance to compute final instance
            'settings': [],     # Optional, list of dict, settings that could be set through UI
            'alarms': [],       # Optional, list of dict, raisable alarms
        }

    def init(self, role_service):
        """
        Optional override. Executed during device initialization, after role settings and alarms
        have been set, before device specific initialization.
        """

    def update_data(self, role_service, sensor_data: dict):
        """
        Optional override. Executed after the data parsing, before updating them on service Dbus.
        Can be used to add or modify data depending on settings or custom methods.
        """
        pass

    @staticmethod
    def get_class(role_name: str):
        return BleRole.ROLE_CLASSES.get(role_name, None)

    @staticmethod
    def load_classes(execution_path: str):
        role_classes_prefix = f"{os.path.splitext(os.path.basename(__file__))[0]}_"

        # Loading manufacturer specific classes
        for filename in os.listdir(os.path.dirname(execution_path)):
            if filename.startswith(role_classes_prefix) and filename.endswith('.py'):
                file_path = os.path.join(os.path.dirname(execution_path), filename)
                module_name = os.path.splitext(filename)[0]

                # Import the module from file
                spec = importlib.util.spec_from_file_location(module_name, file_path)
                if spec is None or spec.loader is None:
                    logging.error(f"Failed to get spec for role class {module_name!r}@{file_path!r}")
                    continue
                module = importlib.util.module_from_spec(spec)
                try:
                    spec.loader.exec_module(module)
                except Exception:
                    logging.exception(f"Failed to import role class {module_name!r}@{file_path!r}")
                    continue

                # Check and import
                for name, obj in inspect.getmembers(module, inspect.isclass):
                    if obj.__module__ == module.__name__ and issubclass(obj, BleRole) and obj is not BleRole:
                        name = getattr(obj, 'NAME', None)
                        if not name:
                            logging.error(f"Role class {module_name!r}@{file_path!r} has invalid NAME: {name!r}")
                            continue
                        if name in BleRole.ROLE_CLASSES:
                            prev = BleRole.ROLE_CLASSES[name].__name__
                            logging.error(
                                f"Role {name!r} in {module_name!r}@{file_path!r} is already registered in {prev!r}, ignoring it")
                            continue
                        BleRole.ROLE_CLASSES[name] = obj
                        break
        logging.info(f"Role classes: {BleRole.ROLE_CLASSES!r}")

    def check_configuration(self):
        self._plog = f"{self.NAME}:"
        for key in ['dev_instance', 'settings', 'alarms']:
            if key not in self.info:
                raise ValueError(f"{self._plog} configuration '{key}' is missing")
            if self.info[key] is None:
                raise ValueError(f"{self._plog} Configuration '{key}' can not be None")

        for number in ['dev_instance']:
            if not isinstance(self.info[number], int):
                raise ValueError(f"{self._plog} Configuration '{number}' must be an integer")

        for list_key in ['settings', 'alarms']:
            if not isinstance(self.info[list_key], list):
                raise ValueError(f"{self._plog} Configuration '{list_key}' must be a list")

        for index, setting in enumerate(self.info['settings']):
            if 'name' not in setting:
                raise ValueError(f"{self._plog} Missing 'name' in setting at index {index}")
            if 'props' not in setting:
                raise ValueError(f"{self._plog} Missing 'props' definition in setting {setting['name']}")
            for key in ['type', 'def']:
                if key not in setting['props']:
                    raise ValueError(f"{self._plog} Missing key '{key}' in setting {setting['name']}")
            if setting['props']['type'].is_int():
                for int_key in ['min', 'max']:
                    if int_key not in setting['props']:
                        raise ValueError(f"{self._plog} Missing key '{int_key}' in setting {setting['name']}")

        for index, alarm in enumerate(self.info['alarms']):
            if 'name' not in alarm:
                raise ValueError(f"{self._plog} Missing 'name' in alarm at index {index}")
            for key in ['name', 'update']:
                if key not in alarm:
                    raise ValueError(f"{self._plog} Missing key '{key}' in alarm {alarm['name']}")
