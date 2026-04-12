"""Shared test fixtures: stub D-Bus and GLib modules unavailable off-device."""
import sys
import os
import types

_src = os.path.join(os.path.dirname(__file__), '..')
_ext = os.path.join(_src, 'ext')
_velib = os.path.join(_ext, 'velib_python')
for p in (_src, _ext, _velib):
    if p not in sys.path:
        sys.path.insert(0, p)

# Stub modules that require system D-Bus / GLib (not available off-device).
# Import chain: ble_device -> dbus_ble_service -> dbus_settings_service -> vedbus -> dbus
# Import chain: ble_role_digitalinput -> gi.repository.GLib
_stub_names = (
    'dbus', 'dbus.bus', 'dbus.mainloop', 'dbus.mainloop.glib',
    'dbus.service', 'dbus.exceptions',
    'gi', 'gi.repository',
    'vedbus', 'settingsdevice',
    'dbus_settings_service', 'dbus_ble_service', 'dbus_role_service',
)
for _name in _stub_names:
    if _name not in sys.modules:
        sys.modules[_name] = types.ModuleType(_name)

# Wire up dbus attribute hierarchy so `dbus.bus.BusConnection` resolves
import dbus as _dbus_stub  # noqa: E402
_dbus_stub.SystemBus = lambda **kw: None
_dbus_stub.SessionBus = lambda **kw: None
_dbus_stub.Interface = lambda *a, **kw: None
_dbus_stub.String = str
_dbus_stub.Bus = type('Bus', (), {})

_dbus_bus_stub = sys.modules['dbus.bus']
_dbus_bus_stub.BusConnection = type('BusConnection', (), {
    'TYPE_SYSTEM': 'system',
    'TYPE_SESSION': 'session',
    '__new__': lambda cls, *a, **kw: object.__new__(cls),
    'get_is_connected': lambda self: True,
})
_dbus_stub.bus = _dbus_bus_stub

# GLib stub with enough surface for BleRoleDigitalInput (timeout_add_seconds)
_gi_repo = sys.modules['gi.repository']
_gi_repo.GLib = type('GLib', (), {
    'timeout_add_seconds': staticmethod(lambda *a, **kw: None),
    'idle_add': staticmethod(lambda *a, **kw: None),
})()

# DbusBleService fake
import dbus_ble_service as _dbs_stub  # noqa: E402
_fake_ble_svc = type('FakeBleService', (), {
    'register_role_service': lambda self, *a: None,
    'unregister_role_service': lambda self, *a: None,
    '_get_value': lambda self, *a: 1,
})()
_dbs_stub.DbusBleService = type(
    'DbusBleService', (), {'get': staticmethod(lambda: _fake_ble_svc)}
)

# DbusRoleService fake
import dbus_role_service as _drs_stub  # noqa: E402
_drs_stub.DbusRoleService = type('DbusRoleService', (), {})

# vedbus fakes
import vedbus as _vedbus_stub  # noqa: E402
_vedbus_stub.VeDbusService = type('VeDbusService', (), {})
_vedbus_stub.VeDbusItemImport = type('VeDbusItemImport', (), {})
_vedbus_stub.VeDbusItemExport = type('VeDbusItemExport', (), {})
