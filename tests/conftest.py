"""
Pytest configuration for the BLE-charger test suite.

Tests are deliberately self-contained — they exercise the pure logic
and the mixin behaviours against captured byte fixtures.  They do NOT
require D-Bus, BlueZ, or a live device.  The two driver modules
(``ble_device_ip22_charger``, ``ble_device_orion_tr``) pull in dbus and
GLib at import time, so we provide minimal stub modules so the
shared-helper module (``ble_charger_common``) can import cleanly in a
test environment.

When running the suite:

    cd venus-os-dbus-ble-sensors-py
    PYTHONPATH=src/opt/victronenergy/dbus-ble-sensors-py:tests \\
        python3 -m pytest tests/ -v

Or via the wrapper script ``tests/run.sh`` if you don't want to type
the path.
"""
from __future__ import annotations

import os
import sys
import types

# Make the shared module importable without dragging in dbus/glib.
HERE = os.path.dirname(os.path.abspath(__file__))
DRIVER_DIR = os.path.normpath(os.path.join(
    HERE, "..", "src", "opt", "victronenergy", "dbus-ble-sensors-py"))
sys.path.insert(0, DRIVER_DIR)

# Stub out the heavy runtime imports ble_charger_common touches at
# module level (dbus, gi.repository.GLib, orion_tr_gatt,
# scan_control).  Tests that need real behaviour from these get
# explicit fakes via fixtures below.

if "dbus" not in sys.modules:
    dbus = types.ModuleType("dbus")
    dbus.SystemBus = lambda: None
    dbus.SessionBus = lambda: None
    dbus.Interface = lambda *a, **kw: None
    dbus.DBusException = Exception
    sys.modules["dbus"] = dbus

if "gi" not in sys.modules:
    gi = types.ModuleType("gi")
    gi_repo = types.ModuleType("gi.repository")

    class _GLibStub:
        # Minimal facade — capture timeout_add invocations so tests
        # can assert scheduling behaviour without a real main loop.
        scheduled: list[tuple[int, object]] = []

        @classmethod
        def timeout_add(cls, ms, fn):
            cls.scheduled.append((ms, fn))
            return 0

    gi_repo.GLib = _GLibStub
    sys.modules["gi"] = gi
    sys.modules["gi.repository"] = gi_repo

# orion_tr_gatt provides AsyncGATTWriter — replace with a stub that
# tests can introspect for write_register calls.
if "orion_tr_gatt" not in sys.modules:
    otg = types.ModuleType("orion_tr_gatt")

    class _StubAsyncGATTWriter:
        def __init__(self, *a, **kw):
            self.busy = False
            self.calls: list[dict] = []

        def write_register(self, mac, passkey, register_id, value_bytes,
                           on_done=None):
            self.calls.append({
                "mac": mac,
                "passkey": passkey,
                "register_id": register_id,
                "value_bytes": bytes(value_bytes),
                "on_done": on_done,
            })
            # Default behaviour: report immediate success unless the
            # test wires .next_result = False.
            if on_done is not None:
                on_done(getattr(self, "next_result", True))

    otg.AsyncGATTWriter = _StubAsyncGATTWriter
    sys.modules["orion_tr_gatt"] = otg

if "scan_control" not in sys.modules:
    sc = types.ModuleType("scan_control")
    sc.pause_scanning = lambda *a, **kw: None
    sc.resume_scanning = lambda *a, **kw: None
    sys.modules["scan_control"] = sc

# pytest fixtures — real ones, not stubs.
import pytest  # noqa: E402

class FakeRoleService:
    """Drop-in for ``DbusRoleService``-shaped objects in unit tests.

    Behaves as a dict-by-path: ``rs[path] = value`` writes, ``rs[path]``
    reads.  Reads on an unwritten path raise ``KeyError`` so the
    mixin's ``KeyError`` fallback in ``_publish_alarms`` is exercised.
    """

    def __init__(self, initial: dict | None = None):
        self.values: dict[str, object] = dict(initial or {})

    def __setitem__(self, key, value):
        self.values[key] = value

    def __getitem__(self, key):
        return self.values[key]

    def __contains__(self, key):
        return key in self.values

class FakeDbusSettings:
    """In-memory stand-in for ``DbusSettingsService``."""

    def __init__(self, initial: dict | None = None):
        self.values: dict[str, object] = dict(initial or {})
        self.created: list[str] = []

    def set_item(self, path, def_value=None, min_value=0, max_value=0,
                 silent=False, callback=None):
        if path not in self.values:
            self.values[path] = def_value
            self.created.append(path)
        return self  # not a real VeDbusItemImport, but tests don't need it

    def set_value(self, path, value):
        self.values[path] = value

    def try_get_value(self, path):
        return self.values.get(path)

    def get_value(self, path):
        return self.values.get(path)

@pytest.fixture
def fake_role():
    """A pristine FakeRoleService each test."""
    return FakeRoleService()

@pytest.fixture
def fake_settings():
    """A pristine FakeDbusSettings each test."""
    return FakeDbusSettings()

@pytest.fixture
def writer():
    """A fresh stub AsyncGATTWriter each test."""
    from orion_tr_gatt import AsyncGATTWriter
    w = AsyncGATTWriter()
    yield w

@pytest.fixture(autouse=True)
def reset_glib_scheduled():
    """Clear GLib.timeout_add capture between tests."""
    from gi.repository import GLib
    GLib.scheduled.clear()
    yield
    GLib.scheduled.clear()
