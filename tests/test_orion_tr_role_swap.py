"""
Test the role-swap mechanics on the Orion-TR driver.

Specifically the Enabled-flag mirror added in gap #5 — without it, a
device swap from dcdc → alternator leaves the new role's D-Bus service
disconnected until the user manually re-toggles Enabled in gui-v2.
"""
from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest

@pytest.fixture(scope="module")
def orion_module():
    """Import ble_device_orion_tr with the dependency stubs already
    set up by conftest.py + the filter-test fixture.  Idempotent."""
    # The filter-test module already installs these stubs at import
    # time; re-running them here is a no-op but makes this file
    # self-contained.
    if "vedbus" not in sys.modules:
        vedbus = types.ModuleType("vedbus")
        vedbus.VeDbusItemImport = type("VeDbusItemImport", (), {})
        vedbus.VeDbusItemExport = type("VeDbusItemExport", (), {})
        vedbus.VeDbusService = type("VeDbusService", (), {})
        sys.modules["vedbus"] = vedbus
    if "dbus_bus" not in sys.modules:
        dbus_bus = types.ModuleType("dbus_bus")
        dbus_bus.get_bus = lambda _name: types.SimpleNamespace(
            list_names=lambda: [_name])
        sys.modules["dbus_bus"] = dbus_bus
    for name in ("dbus_ble_service", "dbus_role_service", "ble_device",
                 "ble_role", "orion_tr_pin", "orion_tr_key_settings",
                 "ip22_key_settings"):
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)
    if not hasattr(sys.modules["ble_device"], "BleDevice"):
        sys.modules["ble_device"].BleDevice = type("BleDevice", (), {
            "MANUFACTURER_ID": None,
            "DEVICE_CLASSES": {},
            "info": {},
        })
    if not hasattr(sys.modules["ble_role"], "BleRole"):
        sys.modules["ble_role"].BleRole = type("BleRole", (), {})
    if not hasattr(sys.modules["dbus_role_service"], "DbusRoleService"):
        sys.modules["dbus_role_service"].DbusRoleService = type(
            "DbusRoleService", (), {})
    if not hasattr(sys.modules["dbus_ble_service"], "DbusBleService"):
        sys.modules["dbus_ble_service"].DbusBleService = type(
            "DbusBleService", (), {})
    for fn in ("get_advertisement_key", "set_advertisement_key",
               "get_firmware_version", "set_firmware_version",
               "get_preferred_adapter", "set_preferred_adapter",
               "advertisement_key_setting_path"):
        if not hasattr(sys.modules["orion_tr_key_settings"], fn):
            setattr(sys.modules["orion_tr_key_settings"], fn,
                    lambda *a, **kw: None)
    if not hasattr(sys.modules["orion_tr_pin"], "resolve_pairing_passkey"):
        sys.modules["orion_tr_pin"].resolve_pairing_passkey = (
            lambda _s: 14916)
    if "ve_types" not in sys.modules:
        vt = types.ModuleType("ve_types")
        vt.VE_UN8 = int
        sys.modules["ve_types"] = vt
    try:
        import victron_ble  # noqa: F401
        from victron_ble.devices import detect_device_type  # noqa: F401
    except Exception:
        vb = types.ModuleType("victron_ble")
        vb_devices = types.ModuleType("victron_ble.devices")
        vb_devices.detect_device_type = lambda _b: None
        vb_exc = types.ModuleType("victron_ble.exceptions")
        vb_exc.AdvertisementKeyMismatchError = type(
            "AdvertisementKeyMismatchError", (Exception,), {})
        sys.modules["victron_ble"] = vb
        sys.modules["victron_ble.devices"] = vb_devices
        sys.modules["victron_ble.exceptions"] = vb_exc

    import importlib
    return importlib.import_module("ble_device_orion_tr")

@pytest.fixture
def orion_tr_class(orion_module):
    return orion_module.BleDeviceOrionTR

def _build_self(orion_tr_class):
    """Construct a minimal carrier with real bound staticmethods so
    self._enabled_setting_path() resolves through the actual
    implementation, not a MagicMock auto-attribute."""
    class _Stub:
        _plog = "test:orion"
        info = {"dev_id": "orion_tr_ff13422b7a4b",
                "dev_mac": "ff13422b7a4b"}
        # Re-bind the staticmethod so the helper resolves it via the
        # class lookup rather than __getattr__ on a Mock.
        _enabled_setting_path = staticmethod(
            orion_tr_class._enabled_setting_path)

    self = _Stub()
    self._role_services = {}
    self._dbus_settings = MagicMock()
    return self

def test_enabled_setting_path_format(orion_tr_class):
    p = orion_tr_class._enabled_setting_path("orion_tr_xx", "alternator")
    assert p == "/Settings/Devices/orion_tr_xx/alternator/Enabled"

def test_carry_enabled_flag_does_nothing_when_old_role_disabled(
        orion_module, orion_tr_class):
    self = _build_self(orion_tr_class)
    self._role_services = {"dcdc": object()}
    self._dbus_settings.get_value.return_value = 0  # dcdc role disabled

    orion_module.BleDeviceOrionTR._carry_enabled_flag_to(
        self, "alternator")

    # No new path was created.
    self._dbus_settings.set_item.assert_not_called()
    self._dbus_settings.set_value.assert_not_called()

def test_carry_enabled_flag_propagates_to_new_role(
        orion_module, orion_tr_class):
    self = _build_self(orion_tr_class)
    self._role_services = {"dcdc": object()}
    # dcdc role is enabled in settings.
    self._dbus_settings.get_value.return_value = 1

    orion_module.BleDeviceOrionTR._carry_enabled_flag_to(
        self, "alternator")

    # The alternator-role Enabled path was created and set to 1.
    new_path = ("/Settings/Devices/orion_tr_ff13422b7a4b/"
                "alternator/Enabled")
    self._dbus_settings.set_item.assert_called_once_with(
        new_path, 1, 0, 1, silent=True)
    self._dbus_settings.set_value.assert_called_once_with(new_path, 1)

def test_carry_enabled_flag_handles_get_value_exception(
        orion_module, orion_tr_class):
    self = _build_self(orion_tr_class)
    self._role_services = {"dcdc": object()}
    self._dbus_settings.get_value.side_effect = RuntimeError("settings down")

    # Must not raise — failure to read the old flag falls through to
    # "no previous role enabled" and the swap proceeds without
    # touching settings.
    orion_module.BleDeviceOrionTR._carry_enabled_flag_to(
        self, "alternator")
    self._dbus_settings.set_item.assert_not_called()

def test_carry_enabled_flag_no_previous_roles(orion_module, orion_tr_class):
    """First-ever role registration — no roles in _role_services to
    inherit from.  Helper must be a no-op."""
    self = _build_self(orion_tr_class)
    self._role_services = {}
    orion_module.BleDeviceOrionTR._carry_enabled_flag_to(
        self, "alternator")
    self._dbus_settings.set_item.assert_not_called()

def test_carry_enabled_flag_handles_string_truthy_value(
        orion_module, orion_tr_class):
    """``com.victronenergy.settings`` returns int but be defensive
    against a string ``"1"`` showing up.  ``int(...)`` coerces."""
    self = _build_self(orion_tr_class)
    self._role_services = {"dcdc": object()}
    self._dbus_settings.get_value.return_value = "1"

    orion_module.BleDeviceOrionTR._carry_enabled_flag_to(
        self, "alternator")

    self._dbus_settings.set_value.assert_called_once()
