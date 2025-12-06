from __future__ import annotations
import os
import inspect
import logging
import importlib.util
from functools import partial
from dbus_ble_service import DbusBleService
from dbus_role_service import DbusRoleService
from ble_role import BleRole
from ve_types import *


class BleDevice(object):
    """
    Device base class.

    Children class:
        - must overload class variable 'MANUFACTURER_ID' and 'configure' method with self.info.update
    method to overload entries as described in code.
        - can overload 'update_data' method to add post parsing custom logic
    """

    MANUFACTURER_ID = None  # To be overloaded in children classes: int, ble manufacturer id

    # Dict of devices classes, key is manufacturer id
    DEVICE_CLASSES = {}

    def __init__(self, dev_mac: str):
        self._role_services: dict = {}
        self._plog: str = None

        # Mandatory fields must be overloaded by subclasses, optional ones can be left as is.
        self.info = {
            'dev_mac': dev_mac,         # Internal
            'product_id': 0x0000,       # Mandatory, int, custom product id. As no product ID list exists, invent one.
            'product_name': None,       # Mandatory, str, general manufacturer product name, i.e. 'Mopeka sensor'
            'device_name': None,         # Mandatory, str, UI default device name, i.e. 'Mopeka LPG'
            'hardware_version': '1.0.0',  # Optional,  str, Device hardware version
            'firmware_version': '1.0.0',  # Optional,  str, Device firmware version
            'dev_prefix': None,         # Mandatory, str, device prefix, used in dbus path, must be short, without spaces
            # Mandatory, list of dict, keys are role names (temperature, tank, digitalinput, ...) values are dict of configs used in role initialization
            'roles': {},
            'regs': [],                 # Mandatory, list of dict, device advertising data, defined with :
                                        # - offset : mandatory, byte offset, i.e. data start position
                                        # - type   : mandatory, type of the data, cf. ve_types.py
                                        # - bits   : length of the data in bits, mandatory if type is not set
                                        # - shift  : bit offset, in case the data is not "byte aligned"
                                        # - scale  : scale to divide the value with
                                        # - bias   : bias to add to the value
                                        # - flags  : can be : REG_FLAG_BIG_ENDIAN, REG_FLAG_INVALID
                                        # - xlate  : custom method to be executed after data parsing
                                        # - inval  : if flag REG_FLAG_INVALID is set, value that invalidates the data
                                        # - roles  : list of role names concerned by the data. If not defined, all roles, if contains None, data is ignored.
            'settings': [],             # Optional,  list of dict, settings that could be set through UI
            'alarms': [],               # Optional,  list of dict, raisable alarms, defined with :
                                        # - name   : Name of the alarm
                                        # - update : method returning, depending on which alarm it is:
                                        #       - 0 : no alarm
                                        #       - 1 : alarm or warning
                                        #       - 2 : alarm
        }

    def configure(self, manufacturer_data: bytes):
        """
        Mandatory overload, use self.info.update() to add specific configuration.
        """
        raise NotImplementedError("Device class must be configured")

    def check_manufacturer_data(self, manufacturer_data: bytes) -> bool:
        """
        Optional override. Executed at just after manufacturer advertising data reception, to check if the data
        are worth parsing. Return True to continue with parsing, False to ignore the advertisement. 
        """
        return True

    def update_data(self, role_service: DbusRoleService, sensor_data: dict):
        """
        Optional overload. Executed after data parsing, before updating them on service Dbus.
        Can be used to add or modify data depending on settings or custom methods.
        """
        pass

    @staticmethod
    def load_classes(execution_path: str):
        device_classes_prefix = f"{os.path.splitext(os.path.basename(__file__))[0]}_"

        # Loading manufacturer specific classes
        for filename in os.listdir(os.path.dirname(execution_path)):
            if filename.startswith(device_classes_prefix) and filename.endswith('.py'):
                file_path = os.path.join(os.path.dirname(execution_path), filename)
                module_name = os.path.splitext(filename)[0]

                # Import the module from file
                spec = importlib.util.spec_from_file_location(module_name, file_path)
                if spec is None or spec.loader is None:
                    logging.error(f"Failed to get spec for device class {module_name!r}@{file_path!r}")
                    continue
                module = importlib.util.module_from_spec(spec)
                try:
                    spec.loader.exec_module(module)
                except Exception:
                    logging.exception(f"Failed to import device class {module_name!r}@{file_path!r}")
                    continue

                # Check and import
                for name, obj in inspect.getmembers(module, inspect.isclass):
                    if obj.__module__ == module.__name__ and issubclass(obj, BleDevice) and obj is not BleDevice:
                        man_id = getattr(obj, 'MANUFACTURER_ID', None)
                        if not isinstance(man_id, int):
                            logging.error(
                                f"Device class {module_name!r}@{file_path!r} has invalid MANUFACTURER_ID: {man_id!r}")
                            continue
                        if man_id in BleDevice.DEVICE_CLASSES:
                            prev = BleDevice.DEVICE_CLASSES[man_id].__name__
                            logging.error(
                                f"Manufacturer id {man_id!r} in {module_name!r}@{file_path!r} is already registered in {prev!r}, ignoring it")
                            continue
                        BleDevice.DEVICE_CLASSES[man_id] = obj
                        break
        logging.info(f"Device classes: {BleDevice.DEVICE_CLASSES!r}")

    def _load_configuration(self):
        self.info['manufacturer_id'] = self.MANUFACTURER_ID
        self.info['device_name'] = self.info['device_name'] + ' ' + self.info['dev_mac'][-6:].upper()
        self._plog = f"{self.info['dev_mac']} - {self.info['device_name']}:"

        for key in ['manufacturer_id', 'product_id', 'product_name', 'device_name', 'dev_prefix', 'roles', 'regs', 'settings', 'alarms']:
            if key not in self.info:
                raise ValueError(f"{self._plog} configuration '{key}' is missing")
            if self.info[key] is None:
                raise ValueError(f"{self._plog} Configuration '{key}' can not be None")

        for number in ['manufacturer_id', 'product_id']:
            if not isinstance(self.info[number], int):
                raise ValueError(f"{self._plog} Configuration '{number}' must be an integer")

        for list_key in ['regs', 'settings', 'alarms']:
            if not isinstance(self.info[list_key], list):
                raise ValueError(f"{self._plog} Configuration '{list_key}' must be a list")

        for dict_key in ['roles']:
            if not isinstance(self.info[dict_key], dict):
                raise ValueError(f"{self._plog} Configuration '{dict_key}' must be a dict")

        for collection_mandatory in ['roles', 'regs']:
            if self.info[collection_mandatory].__len__() < 1:
                raise ValueError(f"{self._plog} Configuration '{collection_mandatory}' must have at least one element")

        for role_name in self.info['roles'].keys():
            if role_name not in BleRole.ROLE_CLASSES:
                raise ValueError(f"{self._plog} Unknown role '{role_name}'")

        for index, reg in enumerate(self.info['regs']):
            if 'name' not in reg:
                raise ValueError(f"{self._plog} Missing 'name' in reg at index {index}")
            for key in ['type', 'offset']:
                if key not in reg:
                    raise ValueError(f"{self._plog} Missing key '{key}' in reg {reg['name']}")
            if (reg_type := reg['type']) not in VeDataBasicType:
                raise ValueError(f"{self._plog} Data type {reg_type} in reg {reg['name']} is not allowed")
            if reg_type == VE_HEAP_STR:
                if (bits := reg.get('bits', None)) is None:
                    raise ValueError(f"{self._plog} missing 'bits' in reg {reg['name']}")
                elif not isinstance(bits, int):
                    raise ValueError(f"{self._plog} 'bits' in reg {reg['name']} must be an integer")
                elif bits % 8 != 0:
                    raise ValueError(f"{self._plog} 'bits' in reg {reg['name']} must be a multiple of 8")
            if 'roles' in reg:
                for role_name in reg['roles']:
                    if role_name is not None and role_name not in BleRole.ROLE_CLASSES:
                        raise ValueError(f"{self._plog} Unknown role '{role_name}' in reg {reg['name']}")
                    if role_name is not None and role_name not in self.info['roles']:
                        raise ValueError(
                            f"{self._plog} Role '{role_name}' in reg {reg['name']} is not defined in device roles")
            if 'bits' in reg and not isinstance(reg['bits'], int):
                raise ValueError(f"{self._plog} 'bits' in reg {reg['name']} must be an integer")

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

        self.info['dev_id'] = self.info['dev_prefix'] + '_' + self.info['dev_mac']

    def init(self):
        # Setting configuration
        self._load_configuration()
        logging.debug(f"{self._plog} initializing device ...")

        # Init role services
        for role_name, role_config in self.info['roles'].items():
            role = BleRole.get_class(role_name)(role_config)
            try:
                role.check_configuration()
            except ValueError as e:
                logging.error(f"{self._plog} ignoring role {role_name!r}: configuration error: {e}")
                continue
            # Initializing Dbus service
            role_service = DbusRoleService(self, role)
            role_service.load_settings()
            self._role_services[role_name] = role_service
            # Creating entries in ble service to enable/disable options
            DbusBleService.get().register_role_service(role_service)
        logging.debug(f"{self._plog} initialized")

    def _load_str(self, reg: dict, manufacturer_data: bytes) -> str:
        # Check there is enough data
        offset: int = reg['offset']
        size = (reg['bits'] + 7) >> 3
        if size > len(manufacturer_data) - offset:
            logging.error(
                f"{self._plog} can not parse {reg['name']!r}, field is longer than manufacturer data, ignoring it")
            return None

        raw = manufacturer_data[offset:offset + size]
        raw = raw.rstrip(b'\x00')  # Common in BLE: fixed-length NUL-padded strings

        try:
            return raw.decode(encoding='utf-8')
        except UnicodeDecodeError:
            logging.error(f"{self._plog} can not decode {reg['name']!r} as UTF-8, ignoring it")
            return None

    def _load_number(self, reg: dict, manufacturer_data: bytes) -> object:  # int | float | None
        """
        Parse a numerical value from reg definition. Returns an int or a float.
        """
        offset: int = reg['offset']
        flags: list = reg.get('flags', [])
        shift: int = reg.get('shift', None)
        _type = reg['type']

        # Get data length
        if (bits := reg.get('bits', None)) is None:
            bits = _type.int_size() * 8

        # Check there is enough data
        size = (bits + (shift if shift is not None else 0) + 7) >> 3
        if size > len(manufacturer_data) - offset:
            logging.error(
                f"{self._plog} can not parse {reg['name']!r}, field is longer than manufacturer data, ignoring it")
            return None

        # Get raw value
        value = int.from_bytes(
            manufacturer_data[offset:offset + size],
            byteorder='big' if 'REG_FLAG_BIG_ENDIAN' in flags else 'little'
        )

        # Shifting bits
        if shift is not None:
            value = (value >> shift)

        # Extracting bits
        value = value & ((1 << bits) - 1)

        # Applying signed value if needed
        if _type.is_int_signed():
            signing_bit = 1 << (bits - 1)
            if value & signing_bit:
                value -= (1 << bits)

        # Post actions
        if scale := reg.get('scale', None):
            value = value / scale
        if bias := reg.get('bias', None):
            value = value + bias
        if xlate := reg.get('xlate', None):
            value = xlate(value)
        if 'REG_FLAG_INVALID' in flags and value == reg.get('inval', None):
            value = None
        return value

    def _parse_manufacturer_data(self, manufacturer_data: bytes) -> dict:
        values = {}
        for role in self.info['roles']:
            values[role] = {}
        for reg in self.info['regs']:
            value = None
            if (_type := reg['type']).is_int():
                value = self._load_number(reg, manufacturer_data)
            elif _type == VE_HEAP_STR:
                value = self._load_str(reg, manufacturer_data)
            elif _type == VE_FLOAT:
                logging.error(f"{self._plog} can not parse 'VE_FLOAT' type items")

            if value is None:
                continue

            if (roles := reg.get('roles', None)) and None in roles:
                continue
            if roles is None:
                roles = self.info['roles']

            for role in roles:
                values[role][(reg['name'])] = value
        return values

    def _update_dbus_data(self, role_service: DbusRoleService, sensor_data: dict):
        for name, value in sensor_data.items():
            role_service[name] = value
            # TODO Find out what c method veItemSetFmt() do

    def handle_manufacturer_data(self, manufacturer_data: bytes):
        """
        Main data parsing and update method.
        """
        if not DbusBleService.get().is_device_enabled(self.info):
            logging.debug(f"{self._plog} device not enabled, skipping")
            return

        # Parse data
        sensor_data: dict = self._parse_manufacturer_data(manufacturer_data)
        logging.debug(f"{self._plog} data {manufacturer_data!r} parsed: {sensor_data!r}")
        for role_service in self._role_services.values():
            # Filtering data
            role_data = sensor_data[role_service.ble_role.NAME]
            if role_data:
                # Update sensor data from update callbacks
                role_service.ble_role.update_data(role_service, role_data)
                self.update_data(role_service, role_data)

                # Update Dbus with new data
                self._update_dbus_data(role_service, role_data)

            # Update alarm states
            for alarm in role_service.ble_role.info['alarms']:
                role_service.update_alarm(alarm)
            for alarm in self.info['alarms']:
                role_service.update_alarm(alarm)

            # Start service if needed
            role_service.connect()

    def delete(self):
        for role_service in list(self._role_services.values()):
            try:
                role_service.disconnect()
            except Exception:
                logging.exception(f"{self._plog} error disconnecting role service")
            try:
                DbusBleService.get().unregister_role_service(role_service)
            except Exception:
                logging.exception(f"{self._plog} error unregistering role service from BLE service")
        self._role_services.clear()

    def __del__(self):
        self.delete()
