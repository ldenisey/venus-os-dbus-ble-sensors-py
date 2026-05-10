"""Tests for dbus_bus.get_bus() connection caching."""
import os
from unittest.mock import patch

import dbus_bus
import pytest

@pytest.fixture(autouse=True)
def clear_cache():
    """Clear the connection cache between tests."""
    dbus_bus._bus_instances.clear()
    yield
    dbus_bus._bus_instances.clear()

class FakeBus:
    """Minimal stand-in for dbus.bus.BusConnection."""

    def __init__(self, connected=True):
        self._connected = connected

    def get_is_connected(self):
        return self._connected

class TestGetBus:
    def test_returns_same_connection_for_same_key(self):
        bus = FakeBus()
        with patch.object(dbus_bus, 'SystemBus', return_value=bus), \
             patch.dict(os.environ, {}, clear=True):
            first = dbus_bus.get_bus("com.victronenergy.tank.test1")
            second = dbus_bus.get_bus("com.victronenergy.tank.test1")
            assert first is second

    def test_returns_different_connections_for_different_keys(self):
        buses = [FakeBus(), FakeBus()]
        call_count = 0

        def make_bus():
            nonlocal call_count
            b = buses[call_count]
            call_count += 1
            return b

        with patch.object(dbus_bus, 'SystemBus', side_effect=make_bus), \
             patch.dict(os.environ, {}, clear=True):
            first = dbus_bus.get_bus("com.victronenergy.tank.aaa")
            second = dbus_bus.get_bus("com.victronenergy.tank.bbb")
            assert first is not second

    def test_reconnects_when_disconnected(self):
        old_bus = FakeBus(connected=False)
        new_bus = FakeBus(connected=True)

        dbus_bus._bus_instances["com.victronenergy.settings"] = old_bus

        with patch.object(dbus_bus, 'SystemBus', return_value=new_bus), \
             patch.dict(os.environ, {}, clear=True):
            result = dbus_bus.get_bus("com.victronenergy.settings")
            assert result is new_bus
            assert result is not old_bus

    def test_settings_key_shared_across_callers(self):
        bus = FakeBus()
        with patch.object(dbus_bus, 'SystemBus', return_value=bus), \
             patch.dict(os.environ, {}, clear=True):
            from_role_a = dbus_bus.get_bus("com.victronenergy.settings")
            from_role_b = dbus_bus.get_bus("com.victronenergy.settings")
            from_ble_svc = dbus_bus.get_bus("com.victronenergy.settings")
            assert from_role_a is from_role_b is from_ble_svc

    def test_uses_session_bus_when_env_set(self):
        bus = FakeBus()
        with patch.object(dbus_bus, 'SessionBus', return_value=bus) as mock_session, \
             patch.object(dbus_bus, 'SystemBus') as mock_system, \
             patch.dict(os.environ, {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/tmp/test"}):
            result = dbus_bus.get_bus("test.key")
            mock_session.assert_called_once()
            mock_system.assert_not_called()
            assert result is bus

    def test_uses_system_bus_when_env_not_set(self):
        bus = FakeBus()
        with patch.object(dbus_bus, 'SystemBus', return_value=bus) as mock_system, \
             patch.object(dbus_bus, 'SessionBus') as mock_session, \
             patch.dict(os.environ, {}, clear=True):
            os.environ.pop("DBUS_SESSION_BUS_ADDRESS", None)
            result = dbus_bus.get_bus("test.key")
            mock_system.assert_called_once()
            mock_session.assert_not_called()
            assert result is bus

    def test_role_service_keys_dont_collide(self):
        """Simulates two Mopeka tank services — each must get its own bus."""
        buses = [FakeBus(), FakeBus()]
        call_count = 0

        def make_bus():
            nonlocal call_count
            b = buses[call_count]
            call_count += 1
            return b

        with patch.object(dbus_bus, 'SystemBus', side_effect=make_bus), \
             patch.dict(os.environ, {}, clear=True):
            tank_a = dbus_bus.get_bus("com.victronenergy.tank.mopeka_aaa")
            tank_b = dbus_bus.get_bus("com.victronenergy.tank.mopeka_bbb")
            assert tank_a is not tank_b

            # Re-fetching same key returns cached
            assert dbus_bus.get_bus("com.victronenergy.tank.mopeka_aaa") is tank_a
            assert dbus_bus.get_bus("com.victronenergy.tank.mopeka_bbb") is tank_b

    def test_connected_bus_is_not_replaced(self):
        """A connected bus should never be replaced."""
        bus = FakeBus(connected=True)
        dbus_bus._bus_instances["key"] = bus

        new_bus = FakeBus()
        with patch.object(dbus_bus, 'SystemBus', return_value=new_bus), \
             patch.dict(os.environ, {}, clear=True):
            result = dbus_bus.get_bus("key")
            assert result is bus  # original, not replaced
