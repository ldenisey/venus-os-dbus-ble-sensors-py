"""Tests for sensor_rounding.SensorRoundingPolicy."""
import os
import sys

sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..'))

import pytest  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

from sensor_rounding import (  # noqa: E402
    SensorRoundingPolicy,
    DEFAULTS,
    HEARTBEAT_DEFAULT,
    _HEARTBEAT_SETTING_PATH,
    _setting_path,
)


@pytest.fixture
def fake_settings():
    """Stand-in for DbusSettingsService that just records into a dict."""
    storage: dict = {}
    callbacks: dict = {}
    settings = MagicMock(name='DbusSettingsService')

    def set_item(path, default, min_=0, max_=0, callback=None):
        if path not in storage:
            storage[path] = default
        callbacks[path] = callback
        item = MagicMock(name=f'item({path})')
        item.get_value.return_value = storage[path]
        # Expose storage so tests can mutate
        item._storage = storage
        item._callbacks = callbacks
        return item

    settings.set_item.side_effect = set_item
    settings._storage = storage
    settings._callbacks = callbacks
    return settings


def test_initial_settings_created(fake_settings):
    """Every type's setting is created on first construction."""
    SensorRoundingPolicy(fake_settings)
    for ttype in DEFAULTS:
        assert _setting_path(ttype) in fake_settings._storage
    assert _HEARTBEAT_SETTING_PATH in fake_settings._storage


def test_defaults_loaded_into_cache(fake_settings):
    policy = SensorRoundingPolicy(fake_settings)
    assert policy.heartbeat_seconds == HEARTBEAT_DEFAULT
    # Spot-check a couple of types
    assert policy.round_value(23.456, 'temperature') == 23.5     # default 1
    assert policy.round_value(12.345, 'voltage') == 12.35        # default 2
    assert policy.round_value(1013.4, 'pressure') == 1013        # default 0


def test_override_takes_precedence(fake_settings):
    policy = SensorRoundingPolicy(fake_settings)
    # Temperature default 1, override to 2
    assert policy.round_value(23.456, 'temperature', override=2) == 23.46
    # Override with no sensor_type also works
    assert policy.round_value(23.456, override=0) == 23


def test_unknown_sensor_type_passthrough(fake_settings):
    policy = SensorRoundingPolicy(fake_settings)
    assert policy.round_value(23.456, 'frobnitz') == 23.456


def test_no_type_no_override_passthrough(fake_settings):
    policy = SensorRoundingPolicy(fake_settings)
    assert policy.round_value(23.456) == 23.456


def test_none_input_returns_none(fake_settings):
    policy = SensorRoundingPolicy(fake_settings)
    assert policy.round_value(None, 'temperature') is None
    assert policy.round_value(None) is None


def test_non_numeric_input_passthrough(fake_settings):
    policy = SensorRoundingPolicy(fake_settings)
    # Strings can't be rounded; should pass through rather than crash
    assert policy.round_value("hi", 'temperature') == "hi"


def test_zero_is_a_valid_value(fake_settings):
    """Make sure ``0`` doesn't get treated like ``None``."""
    policy = SensorRoundingPolicy(fake_settings)
    assert policy.round_value(0, 'current') == 0
    assert policy.round_value(0.0, 'voltage') == 0.0


def test_settings_change_propagates(fake_settings):
    """When the settings callback fires, the cached value updates."""
    policy = SensorRoundingPolicy(fake_settings)
    # Initial: temperature rounds to 1
    assert policy.round_value(23.456, 'temperature') == 23.5
    # Simulate user editing the setting via dbus-spy / GUI
    cb = fake_settings._callbacks[_setting_path('temperature')]
    cb('com.victronenergy.settings', _setting_path('temperature'),
       {'Value': 0})
    # Now it rounds to 0 decimals
    assert policy.round_value(23.456, 'temperature') == 23


def test_heartbeat_change_propagates(fake_settings):
    policy = SensorRoundingPolicy(fake_settings)
    assert policy.heartbeat_seconds == HEARTBEAT_DEFAULT
    cb = fake_settings._callbacks[_HEARTBEAT_SETTING_PATH]
    cb('com.victronenergy.settings', _HEARTBEAT_SETTING_PATH,
       {'Value': 60})
    assert policy.heartbeat_seconds == 60


def test_get_singleton(fake_settings):
    SensorRoundingPolicy._INSTANCE = None  # reset between tests
    policy = SensorRoundingPolicy(fake_settings)
    assert SensorRoundingPolicy.get() is policy


def test_callback_resilient_to_bad_values(fake_settings):
    """A malformed change payload must not crash the callback."""
    policy = SensorRoundingPolicy(fake_settings)
    cb = fake_settings._callbacks[_setting_path('temperature')]
    # Missing 'Value' key
    cb('com.victronenergy.settings', _setting_path('temperature'), {})
    # Non-numeric string
    cb('com.victronenergy.settings', _setting_path('temperature'),
       {'Value': 'banana'})
    # Cache should still hold a sensible (default) integer
    assert isinstance(policy._cache['temperature'], int)
