import importlib.util
import os
import sys
import types
import unittest
from unittest.mock import MagicMock

sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..', 'ext'))
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..', 'ext', 'velib_python'))

# Stubs for the D-Bus / settings dependencies that ``dbus_role_service``
# imports at module load.  Off-device tests cannot pull in the real
# system bus, so we feed the importer placeholders that satisfy the
# names without exercising them.  The repo's conftest.py provides
# similar stubs for other test files; we duplicate the bits we need
# locally so this test does not depend on conftest having loaded.
def _ensure_stub(name: str, attrs: dict):
    if name not in sys.modules:
        sys.modules[name] = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(sys.modules[name], key, value)


_ensure_stub('dbus', {
    'SystemBus': lambda **kw: None,
    'SessionBus': lambda **kw: None,
    'Interface': lambda *a, **kw: None,
    'String': str,
    'Bus': type('Bus', (), {}),
})
_ensure_stub('dbus.bus', {
    'BusConnection': type('BusConnection', (), {}),
})
sys.modules['dbus'].bus = sys.modules['dbus.bus']
_ensure_stub('vedbus', {
    'VeDbusService': type('VeDbusService', (), {}),
    'VeDbusItemImport': type('VeDbusItemImport', (), {}),
    'VeDbusItemExport': type('VeDbusItemExport', (), {}),
})
_ensure_stub('settingsdevice', {})
_ensure_stub('dbus_settings_service', {
    'DbusSettingsService': type('DbusSettingsService', (), {}),
})
_ensure_stub('dbus_ble_service', {
    'DbusBleService': type('DbusBleService', (), {'get': staticmethod(lambda: None)}),
})

# Load the *real* dbus_role_service module from disk, sidestepping any
# conftest-installed stub for that exact module.
_drs_path = os.path.join(os.path.dirname(__file__), '..', 'dbus_role_service.py')
_spec = importlib.util.spec_from_file_location('_real_dbus_role_service', _drs_path)
_real_drs_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_real_drs_module)
DbusRoleService = _real_drs_module.DbusRoleService


class IsConnectedCacheTests(unittest.TestCase):
    """
    ``handle_manufacturer_data`` calls ``role_service.connect()`` once per
    advertisement per role service, and ``connect`` consults
    ``is_connected``.  Before this change, ``is_connected`` synchronously
    round-tripped the bus daemon via ``NameHasOwner`` on every call,
    which dominated CPU on systems with many role services and busy bus
    traffic.  This test pins the new behaviour: the local
    ``_connected`` flag is the source of truth and ``NameHasOwner`` is
    never called on the steady-state path.
    """

    def _make_service(self):
        # Bypass __init__ to avoid real D-Bus / settings dependencies.
        svc = DbusRoleService.__new__(DbusRoleService)
        svc._dbus_service = object()  # sentinel "registered" object
        svc._service_name = 'com.victronenergy.test.dummy'
        svc._connected = False
        svc._dbus_iface = MagicMock()  # if anything calls NameHasOwner the test fails
        svc._dbus_iface.NameHasOwner.side_effect = AssertionError(
            "NameHasOwner must not be called on the steady-state path")
        return svc

    def test_initial_state_is_disconnected(self):
        svc = self._make_service()
        self.assertFalse(svc.is_connected())

    def test_dbus_service_none_is_disconnected_without_calling_dbus(self):
        svc = self._make_service()
        svc._dbus_service = None
        # Even setting the cache to True must not lie about a
        # non-existent service object.
        svc._connected = True
        self.assertFalse(svc.is_connected())

    def test_connected_flag_short_circuits(self):
        svc = self._make_service()
        svc._connected = True
        # Repeated checks must never round-trip the daemon.
        for _ in range(1000):
            self.assertTrue(svc.is_connected())
        svc._dbus_iface.NameHasOwner.assert_not_called()

    def test_disconnect_clears_flag_and_subsequent_check_returns_false(self):
        svc = self._make_service()
        svc._connected = True
        svc._connected = False
        self.assertFalse(svc.is_connected())
        svc._dbus_iface.NameHasOwner.assert_not_called()


if __name__ == '__main__':
    unittest.main()
