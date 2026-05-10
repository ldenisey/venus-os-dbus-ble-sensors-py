"""
Pin the match-rule budget of ``DbusSettingsService``.

Each ``VeDbusItemImport`` constructed with ``createsignal=True`` (the
default) installs a D-Bus signal match rule on the connection.  The
daemon counts those rules against ``max_match_rules_per_connection``
(default 1024) and does *not* clean them up when the Python object is
garbage-collected.

Before this fix, ``get_item``/``set_item`` could create up to three
``VeDbusItemImport`` objects per cache miss — two of them throwaway
probes — leaking 2 match rules each time.  On a Cerbo with ~19 role
services × ~15 settings each that quietly burned through the 1024
quota and triggered ``LimitsExceeded`` errors mid-init, which in turn
poisoned the multi-sensor ``dev_id`` mutation in
``_create_indexed_role_service`` and started a runaway settings
explosion.

This test asserts the contract: at most **one** signal-bearing
``VeDbusItemImport`` is constructed per cache miss.
"""

import importlib.util
import os
import sys
import types
import unittest
from unittest.mock import MagicMock


sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..', 'ext'))
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..', 'ext', 'velib_python'))


# ---------------------------------------------------------------------------
# Stub D-Bus / vedbus before we import the module under test.
# ---------------------------------------------------------------------------
def _ensure_stub(name: str, attrs: dict):
    if name not in sys.modules:
        sys.modules[name] = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(sys.modules[name], key, value)


_ensure_stub('dbus', {
    'SystemBus': lambda *a, **kw: MagicMock(),
    'SessionBus': lambda *a, **kw: MagicMock(),
    'Bus': type('Bus', (), {}),
    'Int64': int,
    'String': str,
})
_ensure_stub('dbus.bus', {'BusConnection': type('BusConnection', (), {})})
sys.modules['dbus'].bus = sys.modules['dbus.bus']

_ensure_stub('vedbus', {
    'VeDbusItemImport': MagicMock,  # placeholder — replaced per test
    'VeDbusItemExport': type('VeDbusItemExport', (), {}),
})


_module_path = os.path.join(os.path.dirname(__file__), '..', 'dbus_settings_service.py')
_spec = importlib.util.spec_from_file_location('_real_dbus_settings_service', _module_path)
_real_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_real_module)
DbusSettingsService = _real_module.DbusSettingsService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
class _ImportTracker:
    """Stand-in for ``VeDbusItemImport`` that records how it was constructed.

    A construction with ``createsignal=True`` (the default if the kwarg
    is omitted) counts as one match rule against the daemon's
    per-connection limit; ``createsignal=False`` does not.
    """

    def __init__(self):
        self.constructions: list[dict] = []
        self.next_exists: bool = True
        self.next_attrs: tuple = (None, 0, 0, False)

    def __call__(self, bus, service_name, path, *args, **kwargs):
        # ``VeDbusItemImport(bus, service, path, eventCallback=None,
        # createsignal=True)`` — ``eventCallback`` is positional in the
        # real signature for backward compatibility.
        callback = args[0] if args else kwargs.get('eventCallback', None)
        createsignal = kwargs.get('createsignal', True)

        self.constructions.append({
            'path': path,
            'createsignal': createsignal,
            'callback': callback,
        })

        item = MagicMock()
        item.exists = self.next_exists
        item._proxy.GetAttributes.return_value = self.next_attrs
        return item

    @property
    def signal_bearing_count(self) -> int:
        return sum(1 for c in self.constructions if c['createsignal'])


def _make_service(tracker: _ImportTracker) -> DbusSettingsService:
    """Bypass ``__init__`` (which requires a real D-Bus) and patch in
    the tracker as the ``VeDbusItemImport`` constructor seen by the
    module under test."""
    svc = DbusSettingsService.__new__(DbusSettingsService)
    svc._bus = MagicMock()
    svc._paths = {}
    _real_module.VeDbusItemImport = tracker
    return svc


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class GetItemMatchRuleBudget(unittest.TestCase):

    def test_existing_setting_uses_one_match_rule(self):
        tracker = _ImportTracker()
        tracker.next_exists = True
        svc = _make_service(tracker)

        svc.get_item('/Settings/Foo')

        self.assertEqual(tracker.signal_bearing_count, 1,
            f"expected one signal-bearing import, got {tracker.constructions}")

    def test_missing_setting_with_no_default_does_not_subscribe(self):
        tracker = _ImportTracker()
        tracker.next_exists = False
        svc = _make_service(tracker)

        svc.get_item('/Settings/Foo')

        self.assertEqual(tracker.signal_bearing_count, 0,
            f"expected zero signal-bearing imports for non-existing setting "
            f"with no default; got {tracker.constructions}")

    def test_missing_setting_with_default_uses_one_match_rule(self):
        tracker = _ImportTracker()
        tracker.next_exists = False
        svc = _make_service(tracker)

        svc.get_item('/Settings/Foo', def_value=42, min_value=0, max_value=100)

        # Probe (createsignal=False) + parent /Settings probe (createsignal=False) +
        # one keeper (createsignal=True) = 1 rule.
        self.assertEqual(tracker.signal_bearing_count, 1,
            f"expected one signal-bearing import for new setting with default; "
            f"got {tracker.constructions}")

    def test_cache_hit_creates_no_imports(self):
        tracker = _ImportTracker()
        svc = _make_service(tracker)
        svc._paths['/Settings/Foo'] = MagicMock()

        svc.get_item('/Settings/Foo')

        self.assertEqual(len(tracker.constructions), 0)


class SetItemMatchRuleBudget(unittest.TestCase):

    def test_existing_matching_setting_uses_one_match_rule(self):
        tracker = _ImportTracker()
        tracker.next_exists = True
        tracker.next_attrs = (42, 0, 100, False)
        svc = _make_service(tracker)

        svc.set_item('/Settings/Foo', def_value=42, min_value=0, max_value=100)

        self.assertEqual(tracker.signal_bearing_count, 1,
            f"expected one signal-bearing import, got {tracker.constructions}")

    def test_missing_setting_uses_one_match_rule(self):
        tracker = _ImportTracker()
        tracker.next_exists = False
        svc = _make_service(tracker)

        svc.set_item('/Settings/Foo', def_value=42, min_value=0, max_value=100)

        self.assertEqual(tracker.signal_bearing_count, 1,
            f"expected one signal-bearing import for new setting, "
            f"got {tracker.constructions}")

    def test_existing_setting_with_different_attrs_uses_one_match_rule(self):
        tracker = _ImportTracker()
        tracker.next_exists = True
        tracker.next_attrs = (10, 0, 100, False)  # current value differs
        svc = _make_service(tracker)

        svc.set_item('/Settings/Foo', def_value=42, min_value=0, max_value=100)

        self.assertEqual(tracker.signal_bearing_count, 1,
            f"expected one signal-bearing import when attrs need re-init, "
            f"got {tracker.constructions}")


class BulkBudget(unittest.TestCase):

    def test_one_thousand_new_settings_stay_under_1024_rules(self):
        """Provincial sanity check: even a fork like this one with ~19
        role services × ~15 settings each (~285 settings) should stay
        comfortably under the 1024 rule limit.  Test 1000 just to leave
        headroom for anything we add later."""
        tracker = _ImportTracker()
        tracker.next_exists = False
        svc = _make_service(tracker)

        for i in range(1000):
            svc.get_item(f'/Settings/Test/Bulk/{i}', def_value=i, min_value=0, max_value=10000)

        # Each new setting consumes one rule — total <= 1000, well under 1024.
        self.assertLessEqual(tracker.signal_bearing_count, 1024,
            f"signal-bearing imports {tracker.signal_bearing_count} exceed 1024-rule limit")
        self.assertEqual(tracker.signal_bearing_count, 1000,
            f"expected exactly 1000 long-lived imports, got {tracker.signal_bearing_count}")


if __name__ == '__main__':
    unittest.main()
