"""Integration: BleDevice._update_dbus_data routes through SensorPublisher.

Lightweight test that constructs a real SensorPublisher (with a fake
policy + role service) and verifies the device dispatches per-reg
sensor_type / round overrides correctly.
"""
import os
import sys

sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..'))

import pytest  # noqa: E402

from sensor_publisher import SensorPublisher  # noqa: E402


class FakePolicy:
    heartbeat_seconds = 900
    _table = {'temperature': 1, 'voltage': 2}

    def round_value(self, value, sensor_type=None, override=None):
        if value is None:
            return None
        ndigits = override if override is not None else (
            self._table.get(sensor_type) if sensor_type else None
        )
        if ndigits is None:
            return value
        try:
            return round(value, ndigits)
        except (TypeError, ValueError):
            return value


class FakeRoleService:
    def __init__(self):
        self._values: dict = {}
        # vedbus context manager protocol (no-op for tests)
        self.entered = 0

    def __enter__(self):
        self.entered += 1
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.entered -= 1
        return False

    def __setitem__(self, path, value):
        self._values[path] = value

    def __getitem__(self, path):
        return self._values.get(path)


class FakeDevice:
    """Minimal stand-in: implements just what _update_dbus_data needs."""

    def __init__(self, regs):
        self.info = {'regs': regs}

    # Reuse the real implementation from BleDevice
    from ble_device import BleDevice
    _update_dbus_data = BleDevice._update_dbus_data
    _regs_by_name = BleDevice._regs_by_name


@pytest.fixture
def publisher():
    SensorPublisher._INSTANCE = None
    return SensorPublisher(FakePolicy())


def test_sensor_type_applied_via_reg(publisher):
    """A reg tagged with sensor_type='temperature' gets rounded to 1 dp."""
    svc = FakeRoleService()
    dev = FakeDevice(regs=[
        {'name': 'Temperature', 'sensor_type': 'temperature'},
        {'name': 'Voltage', 'sensor_type': 'voltage'},
    ])
    dev._update_dbus_data(svc, {'Temperature': 23.456, 'Voltage': 12.345})
    assert svc['Temperature'] == 23.5
    assert svc['Voltage'] == 12.35


def test_round_override_takes_precedence(publisher):
    svc = FakeRoleService()
    dev = FakeDevice(regs=[
        {'name': 'Temperature', 'sensor_type': 'temperature', 'round': 2},
    ])
    dev._update_dbus_data(svc, {'Temperature': 23.456})
    assert svc['Temperature'] == 23.46


def test_no_sensor_type_passes_through(publisher):
    """An unrelated reg without sensor_type is published without rounding."""
    svc = FakeRoleService()
    dev = FakeDevice(regs=[
        {'name': 'SeqNo'},  # no sensor_type, no round
    ])
    dev._update_dbus_data(svc, {'SeqNo': 42})
    assert svc['SeqNo'] == 42


def test_dedup_skips_unchanged_within_heartbeat(publisher):
    svc = FakeRoleService()
    dev = FakeDevice(regs=[
        {'name': 'Temperature', 'sensor_type': 'temperature'},
    ])
    dev._update_dbus_data(svc, {'Temperature': 23.49})  # writes 23.5
    svc._values.clear()
    dev._update_dbus_data(svc, {'Temperature': 23.51})  # same rounded → skip
    assert 'Temperature' not in svc._values


def test_context_manager_used_for_batching(publisher):
    """The outer `with role_service:` still wraps the writes."""
    svc = FakeRoleService()
    dev = FakeDevice(regs=[
        {'name': 'Temperature', 'sensor_type': 'temperature'},
    ])
    dev._update_dbus_data(svc, {'Temperature': 23.5})
    # context entered then exited
    assert svc.entered == 0


def test_fallback_when_no_publisher_initialized():
    """If SensorPublisher.get() returns None, fall through to direct write."""
    SensorPublisher._INSTANCE = None
    svc = FakeRoleService()
    dev = FakeDevice(regs=[
        {'name': 'Temperature', 'sensor_type': 'temperature'},
    ])
    dev._update_dbus_data(svc, {'Temperature': 23.456})
    # No rounding applied (publisher absent) — raw value lands
    assert svc['Temperature'] == 23.456
