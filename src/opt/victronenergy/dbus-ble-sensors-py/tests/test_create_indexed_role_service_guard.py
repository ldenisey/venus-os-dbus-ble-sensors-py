"""
Pin the contract that ``BleDevice._create_indexed_role_service``
**never mutates** ``self.info['dev_id']``.

The previous design temporarily wrote the indexed dev id into
``self.info['dev_id']`` so that ``DbusRoleService.__init__`` would pick
it up as a side channel.  Any exception during role-service init left
the field corrupt; the next advertisement re-read the corrupt value as
its "base" and appended yet another ``_NN`` — on a multi-sensor device
broadcasting many indices a second (e.g. SeeLevel BTP3) the dev id
grew unboundedly, registering hundreds of bloated paths in
``com.victronenergy.settings``.

The proper fix removes the mutation entirely.  ``DbusRoleService``
takes the indexed dev id as an explicit constructor parameter;
``self.info['dev_id']`` is only ever read, never written.  These tests
assert that contract directly.
"""

import importlib.util
import logging
import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch


sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..', 'ext'))
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..', 'ext', 'velib_python'))


# Stub D-Bus / vedbus / inner services for off-device import.
def _ensure_stub(name: str, attrs: dict):
    if name not in sys.modules:
        sys.modules[name] = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(sys.modules[name], key, value)


_ensure_stub('dbus', {
    'SystemBus': lambda **kw: MagicMock(),
    'SessionBus': lambda **kw: MagicMock(),
    'Interface': lambda *a, **kw: MagicMock(),
    'Bus': type('Bus', (), {}),
    'Int64': int,
    'String': str,
})
_ensure_stub('dbus.bus', {'BusConnection': type('BusConnection', (), {})})
sys.modules['dbus'].bus = sys.modules['dbus.bus']
_ensure_stub('vedbus', {
    'VeDbusService': MagicMock,
    'VeDbusItemImport': MagicMock,
    'VeDbusItemExport': MagicMock,
})
_ensure_stub('settingsdevice', {})
_ensure_stub('dbus_settings_service', {
    'DbusSettingsService': MagicMock,
})
_ensure_stub('dbus_ble_service', {
    'DbusBleService': type('DbusBleService', (), {
        'get': staticmethod(lambda: MagicMock()),
    }),
})
_ensure_stub('dbus_role_service', {
    'DbusRoleService': MagicMock,
})


_ble_device_path = os.path.join(os.path.dirname(__file__), '..', 'ble_device.py')
_spec = importlib.util.spec_from_file_location('_real_ble_device', _ble_device_path)
_real_ble_device = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_real_ble_device)
BleDevice = _real_ble_device.BleDevice


# Bypass the heavy ``__init__`` of BleDevice — we want a direct test of
# ``_create_indexed_role_service`` and need to control ``self.info``
# without dragging in the full settings / D-Bus init.
def _make_device(dev_id: str = 'seelevel_btp3_00a0508d9569') -> BleDevice:
    dev = BleDevice.__new__(BleDevice)
    dev.info = {
        'dev_id': dev_id,
        'dev_prefix': 'seelevel_btp3',
        'dev_mac': '00a0508d9569',
        'roles': {},
        'settings': [],
        'alarms': [],
        'product_id': 0,
        'product_name': '',
        'device_name': '',
        'firmware_version': '',
        'hardware_version': '',
        'manufacturer_id': 0,
    }
    dev._role_services = {}
    dev._plog = '[test]'
    return dev


class DevIdIsNeverMutated(unittest.TestCase):

    def test_dev_id_unchanged_on_role_service_construction_failure(self):
        dev = _make_device()
        original = dev.info['dev_id']

        with patch.object(_real_ble_device, 'BleRole') as mock_role_cls:
            mock_role_cls.get_class.return_value = MagicMock()
            with patch.object(_real_ble_device, 'DbusRoleService',
                              side_effect=RuntimeError('AddMatch budget exhausted')):
                result = dev._create_indexed_role_service('tank', 0)

        self.assertIsNone(result, "should return None on failure")
        self.assertEqual(dev.info['dev_id'], original,
            f"dev_id must not be mutated; got {dev.info['dev_id']!r}")

    def test_dev_id_unchanged_on_load_settings_failure(self):
        dev = _make_device()
        original = dev.info['dev_id']

        fake_role_service = MagicMock()
        fake_role_service.load_settings.side_effect = KeyError('min')

        with patch.object(_real_ble_device, 'BleRole') as mock_role_cls:
            mock_role_cls.get_class.return_value = MagicMock()
            with patch.object(_real_ble_device, 'DbusRoleService',
                              return_value=fake_role_service):
                result = dev._create_indexed_role_service('tank', 1)

        self.assertIsNone(result)
        self.assertEqual(dev.info['dev_id'], original)

    def test_repeated_failures_do_not_compound_corruption(self):
        """The pre-refactor bug: each failed creation read the already-
        mutated ``dev_id`` and appended another ``_NN``.  After 50 cycles
        through the SeeLevel sensor sequence (4 indices each) the dev id
        grew by ~600 chars.  With the refactor the field is never
        written, so even 200 forced failures cannot corrupt it."""
        dev = _make_device()
        original = dev.info['dev_id']

        with patch.object(_real_ble_device, 'BleRole') as mock_role_cls:
            mock_role_cls.get_class.return_value = MagicMock()
            with patch.object(_real_ble_device, 'DbusRoleService',
                              side_effect=RuntimeError('forced failure')):
                for _ in range(50):
                    for idx in (0, 1, 2, 13):
                        dev._create_indexed_role_service('tank', idx)

        self.assertEqual(dev.info['dev_id'], original,
            f"dev_id grew under repeated failure; got len={len(dev.info['dev_id'])}: "
            f"{dev.info['dev_id'][:80]}...")

    def test_dev_id_unchanged_on_success(self):
        """Even the happy path must not touch ``self.info['dev_id']``."""
        dev = _make_device()
        original = dev.info['dev_id']

        with patch.object(_real_ble_device, 'BleRole') as mock_role_cls:
            mock_role_cls.get_class.return_value = MagicMock()
            with patch.object(_real_ble_device, 'DbusRoleService',
                              return_value=MagicMock()):
                result = dev._create_indexed_role_service('tank', 0)

        self.assertIsNotNone(result)
        self.assertEqual(dev.info['dev_id'], original)


class IndexedDevIdPassedToConstructor(unittest.TestCase):
    """Verify the constructor receives the indexed dev id as an
    explicit ``dev_id=`` keyword, rather than reading it back from
    ``ble_device.info['dev_id']`` (which would re-introduce the
    side-channel coupling)."""

    def test_constructor_called_with_indexed_dev_id_keyword(self):
        dev = _make_device(dev_id='seelevel_btp3_00a0508d9569')

        with patch.object(_real_ble_device, 'BleRole') as mock_role_cls:
            mock_role_cls.get_class.return_value = MagicMock()
            with patch.object(_real_ble_device, 'DbusRoleService',
                              return_value=MagicMock()) as ctor:
                dev._create_indexed_role_service('tank', 7)

        ctor.assert_called_once()
        kwargs = ctor.call_args.kwargs
        self.assertIn('dev_id', kwargs,
            f"DbusRoleService must be called with explicit dev_id kwarg; "
            f"got args={ctor.call_args.args}, kwargs={kwargs}")
        self.assertEqual(kwargs['dev_id'], 'seelevel_btp3_00a0508d9569_07')


if __name__ == '__main__':
    unittest.main()
