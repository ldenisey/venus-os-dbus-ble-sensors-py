# Copyright 2026 Clint Goudie-Nice
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""
Load-driven self-throttle for the BLE-sensors service.

The Cerbo runs ``/usr/sbin/watchdog -c /etc/watchdog.conf`` which forces
a hardware reset when the 15-minute load average exceeds 6.  On a busy
gateway with many BLE devices in range plus user-installed Python
services, that limit is routinely close to being crossed — a single
spike can tip it over and reboot the system.

This module monitors ``/proc/loadavg`` and asks the owning service to
**voluntarily suspend** its busiest BLE work (the HCI advertisement
tap + BlueZ passive scan registration) when the load gets dangerous,
then resume when it recovers.

State machine (thresholds as agreed):

    trip:    15-min >= 5.5  OR  5-min >= 6.0
    release: 15-min <  5.0  AND  5-min <  5.0

A single log line is emitted on trip (none on release; the GUI
notification gives the visible signal both ways).

Used by ``dbus_ble_sensors.py``.  Pure stdlib — no D-Bus, no GLib,
no I/O outside ``open('/proc/loadavg')``.  The owning service is
expected to call :meth:`LoadThrottle.tick` periodically (e.g. from
``GLib.timeout_add_seconds(30, ...)``).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Optional


# Threshold values intentionally hard-coded to mirror the cerbo's
# /etc/watchdog.conf ``max-load-15 = 6`` policy.  Trip 0.5 below the
# watchdog's own threshold so we get out of the way before it does.
TRIP_15M: float = 5.5
TRIP_5M: float = 6.0
RELEASE_15M: float = 5.0
RELEASE_5M: float = 5.0


_LOADAVG_PATH = "/proc/loadavg"

_logger = logging.getLogger(__name__)


class LoadThrottle:
    """Tracks ``/proc/loadavg`` and signals when sustained high load
    crosses a trip threshold.

    The owning service registers two callbacks via the constructor —
    ``on_trip`` is invoked once when the throttle becomes active,
    ``on_release`` once when it becomes inactive.  Both are called
    synchronously from :meth:`tick` on the same thread (the GLib
    mainloop in practice), so they don't need to be thread-safe.

    The ``loadavg_path`` parameter exists for tests; production code
    should leave it at the default.
    """

    def __init__(
        self,
        *,
        on_trip: Callable[[float, float], None] | None = None,
        on_release: Callable[[float, float], None] | None = None,
        trip_15m: float = TRIP_15M,
        trip_5m: float = TRIP_5M,
        release_15m: float = RELEASE_15M,
        release_5m: float = RELEASE_5M,
        loadavg_path: str = _LOADAVG_PATH,
    ) -> None:
        self._on_trip = on_trip
        self._on_release = on_release
        self._trip_15m = trip_15m
        self._trip_5m = trip_5m
        self._release_15m = release_15m
        self._release_5m = release_5m
        self._loadavg_path = loadavg_path
        self._throttled: bool = False
        self._last_load_5m: Optional[float] = None
        self._last_load_15m: Optional[float] = None

    @property
    def is_throttled(self) -> bool:
        return self._throttled

    @property
    def last_load_5m(self) -> Optional[float]:
        """Most recent 5-min load avg read by :meth:`tick`."""
        return self._last_load_5m

    @property
    def last_load_15m(self) -> Optional[float]:
        """Most recent 15-min load avg read by :meth:`tick`."""
        return self._last_load_15m

    def _read_loadavg(self) -> tuple[float, float] | None:
        """Return ``(load_5m, load_15m)``.  ``None`` on read failure."""
        try:
            with open(self._loadavg_path, "r") as f:
                parts = f.read().split()
            return float(parts[1]), float(parts[2])
        except (OSError, ValueError, IndexError) as exc:
            _logger.warning("load_throttle: failed to read %s: %s",
                            self._loadavg_path, exc)
            return None

    def tick(self) -> bool:
        """Check the current load and fire callbacks on state change.

        Always returns ``True`` so it can be used directly as a
        ``GLib.timeout_add_seconds`` callback.
        """
        reading = self._read_loadavg()
        if reading is None:
            return True
        load_5m, load_15m = reading
        self._last_load_5m = load_5m
        self._last_load_15m = load_15m

        if not self._throttled:
            if load_15m >= self._trip_15m or load_5m >= self._trip_5m:
                self._throttled = True
                _logger.warning(
                    "load_throttle: tripped (15m=%.2f%s, 5m=%.2f%s) "
                    "— suspending BLE advertisement intake",
                    load_15m, " >= %.2f" % self._trip_15m if load_15m >= self._trip_15m else "",
                    load_5m, " >= %.2f" % self._trip_5m if load_5m >= self._trip_5m else "",
                )
                if self._on_trip is not None:
                    try:
                        self._on_trip(load_5m, load_15m)
                    except Exception:
                        _logger.exception(
                            "load_throttle: on_trip callback raised")
        else:
            if load_15m < self._release_15m and load_5m < self._release_5m:
                self._throttled = False
                # No log on release (per spec — the GUI notification
                # going away is the visible signal).
                if self._on_release is not None:
                    try:
                        self._on_release(load_5m, load_15m)
                    except Exception:
                        _logger.exception(
                            "load_throttle: on_release callback raised")

        return True
