# Copyright 2026 Clint Goudie-Nice
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""
Inject notifications into Venus OS's GUI via
``com.victronenergy.platform`` → ``/Notifications``.

The platform daemon (``venus-platform``) hosts the singular
``/Notifications`` collection that ``gui-v2`` reads.  Its
``/Notifications/Inject`` endpoint takes a single string formatted as
three tab-separated fields::

    "<type>\\t<device_name>\\t<description>"

where ``type`` is:

* ``0`` — WARNING  (orange warning triangle in the status bar)
* ``1`` — ALARM    (red alarm triangle; auto-rings the buzzer)
* ``2`` — NOTIFICATION / informational (no banner)

The endpoint creates a new slot at index ``N`` (where ``N`` is the
prior ``NumberOfNotifications``) and immediately calls
``setActive(false)`` on it.  Callers must then set ``Active=true`` on
the slot to surface the notification under "Active Notifications" in
the GUI.

To clear the notification later, set ``Active=false`` + ``Acknowledged=true``
+ ``Silenced=true`` on the same slot.

This module is a thin wrapper that hides the dance.  If the platform
service isn't available (development bench, unit tests), construction
silently falls back to a no-op handle that logs at debug level — so
the throttle's logic stays correct even without a Cerbo to talk to.
"""

from __future__ import annotations

import logging
from typing import Optional

try:
    import dbus  # type: ignore[import-not-found]
except ImportError:
    dbus = None  # allow import in non-D-Bus environments (CI, dev)


_PLATFORM_SERVICE = "com.victronenergy.platform"
_INJECT_PATH = "/Notifications/Inject"
_COUNT_PATH = "/Notifications/NumberOfNotifications"
_BUSITEM_IFACE = "com.victronenergy.BusItem"


# Notification type IDs, mirroring venus-platform's Notification::Type
# enum in notification.hpp.
TYPE_WARNING = 0
TYPE_ALARM = 1
TYPE_INFO = 2


_logger = logging.getLogger(__name__)


class PlatformNotification:
    """Handle to one notification injected into ``com.victronenergy.platform``.

    Created by :func:`inject`.  Use :meth:`activate` after injection to
    move the slot to the "Active" section (the inject path creates it
    inactive by default — quirk of the upstream API).  Use
    :meth:`dismiss` to clear the notification banner once the
    underlying condition has cleared.
    """

    def __init__(self, bus, slot_index: int) -> None:
        self._bus = bus
        self._index = slot_index

    @property
    def slot_index(self) -> int:
        return self._index

    def _set(self, leaf: str, value) -> None:
        path = "/Notifications/%d/%s" % (self._index, leaf)
        try:
            item = self._bus.get_object(_PLATFORM_SERVICE, path)
            dbus.Interface(item, _BUSITEM_IFACE).SetValue(value)
        except Exception as exc:
            _logger.warning(
                "platform_notifications: SetValue(%s) failed: %s", path, exc)

    def activate(self) -> None:
        """Mark the notification active so the GUI shows the banner."""
        self._set("Active", dbus.Boolean(True))

    def dismiss(self) -> None:
        """Clear the notification: inactive + acknowledged + silenced.

        The notification stays in the GUI's history list so operators
        can review past events; the banner and status-bar icon clear.
        """
        # Active=false first so the slot leaves the "Active Notifications"
        # section before the banner-driving counter decrements.
        self._set("Active", dbus.Boolean(False))
        self._set("Acknowledged", dbus.Boolean(True))
        self._set("Silenced", dbus.Boolean(True))


class _NullNotification:
    """Stand-in returned when D-Bus is unavailable.  All ops are no-ops."""

    slot_index = -1

    def activate(self) -> None:
        _logger.debug("platform_notifications: activate() — no-op (no D-Bus)")

    def dismiss(self) -> None:
        _logger.debug("platform_notifications: dismiss() — no-op (no D-Bus)")


def _get_count(bus) -> int:
    """Read ``NumberOfNotifications`` on the platform service."""
    item = bus.get_object(_PLATFORM_SERVICE, _COUNT_PATH)
    return int(dbus.Interface(item, _BUSITEM_IFACE).GetValue())


def inject(bus, *, type_id: int, device_name: str, description: str
           ) -> PlatformNotification:
    """Inject a notification into ``com.victronenergy.platform``.

    Returns a :class:`PlatformNotification` handle.  Caller should
    invoke :meth:`PlatformNotification.activate` to make the
    notification visible, then :meth:`~PlatformNotification.dismiss`
    when the condition clears.

    If the platform service isn't reachable (no D-Bus, service not
    running), returns a :class:`_NullNotification` whose methods are
    no-ops; the caller's logic stays unchanged.
    """
    if dbus is None:
        _logger.debug("platform_notifications: dbus module unavailable, "
                      "returning null handle")
        return _NullNotification()  # type: ignore[return-value]

    try:
        count_before = _get_count(bus)
        inject_obj = bus.get_object(_PLATFORM_SERVICE, _INJECT_PATH)
        payload = "%d\t%s\t%s" % (type_id, device_name, description)
        rc = dbus.Interface(inject_obj, _BUSITEM_IFACE).SetValue(payload)
        if int(rc) != 0:
            _logger.warning(
                "platform_notifications: Inject SetValue returned %s for "
                "payload %r — returning null handle", rc, payload)
            return _NullNotification()  # type: ignore[return-value]
        # New slot lands at the prior count, per
        # venus-platform/src/notifications.cpp:
        #   int index = mNotifications.size();
        # Edge case: when ``mMaxNotifications`` is reached the oldest
        # slot is recycled and gets a lower index.  We don't try to
        # handle that here — the cerbo default max is well above
        # anything we'd realistically generate from one client.
        return PlatformNotification(bus, slot_index=count_before)
    except Exception as exc:
        _logger.warning(
            "platform_notifications: inject failed: %s — returning null "
            "handle", exc)
        return _NullNotification()  # type: ignore[return-value]
