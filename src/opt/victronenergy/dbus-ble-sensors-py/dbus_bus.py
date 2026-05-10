"""Cached D-Bus connection factory.

BusConnection objects created with DBusGMainLoop as the default main loop
are pinned in memory by C-level GLib watch/timeout references that Python's
GC cannot reach.  Without caching, every call site that creates a new bus
connection leaks a connection to the D-Bus daemon, eventually exhausting
the per-UID connection limit (typically 256 for root).

Usage::

    from dbus_bus import get_bus

    # For a VeDbusService that registers object paths — one connection per
    # service name so that '/' registrations don't collide:
    bus = get_bus("com.victronenergy.tank.mopeka_abc123")
    svc = VeDbusService("com.victronenergy.tank.mopeka_abc123", bus)

    # For settings access — all callers share one connection:
    bus = get_bus("com.victronenergy.settings")
"""

import os
import dbus
import dbus.bus

class SystemBus(dbus.bus.BusConnection):
    def __new__(cls):
        return dbus.bus.BusConnection.__new__(cls, dbus.bus.BusConnection.TYPE_SYSTEM)

class SessionBus(dbus.bus.BusConnection):
    def __new__(cls):
        return dbus.bus.BusConnection.__new__(cls, dbus.bus.BusConnection.TYPE_SESSION)

_bus_instances: dict[str, dbus.bus.BusConnection] = {}

def get_bus(cache_key: str) -> dbus.bus.BusConnection:
    """Return a cached bus connection for *cache_key*, creating one if needed.

    Each unique *cache_key* gets its own ``BusConnection``.  This is
    necessary because ``VeDbusService`` registers D-Bus object paths
    (like ``'/'``) and two services on the same connection would collide.

    Use a stable, well-known name as the key:

    * The service name for ``VeDbusService`` instances
      (e.g. ``"com.victronenergy.tank.mopeka_abc123"``).
    * ``"com.victronenergy.settings"`` for all settings access — all
      callers can share one connection since they only make outgoing
      method calls and don't register object paths.
    """
    bus = _bus_instances.get(cache_key)
    if bus is None or not bus.get_is_connected():
        _bus_instances[cache_key] = (
            SessionBus() if "DBUS_SESSION_BUS_ADDRESS" in os.environ
            else SystemBus()
        )
    return _bus_instances[cache_key]
