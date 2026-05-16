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


_WATCHDOG_CONF = "/etc/watchdog.conf"
_LOADAVG_PATH = "/proc/loadavg"

# Fallback if /etc/watchdog.conf is missing or doesn't set the key.
# Matches the value Victron ships in the stock Venus OS image at the
# time of writing.
_DEFAULT_MAX_LOAD_15 = 6.0

_logger = logging.getLogger(__name__)


def _read_watchdog_max_load_15(path: str = _WATCHDOG_CONF) -> float:
    """Parse ``max-load-15`` out of ``/etc/watchdog.conf``.

    Returns the value as a float, or :data:`_DEFAULT_MAX_LOAD_15` if
    the file is missing, unreadable, or doesn't set the key.  Format
    is the watchdog daemon's own one-option-per-line ``key = value``
    syntax; blank lines and ``#`` comments are tolerated.
    """
    try:
        with open(path, 'r') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if '=' not in line:
                    continue
                key, _, val = line.partition('=')
                if key.strip() == 'max-load-15':
                    try:
                        return float(val.strip())
                    except ValueError:
                        return _DEFAULT_MAX_LOAD_15
    except OSError:
        pass
    return _DEFAULT_MAX_LOAD_15


def _derive_thresholds(max_load_15: float) -> tuple[float, float, float, float]:
    """Return ``(trip_15m, trip_5m, release_15m, release_5m)``.

    Strategy: derive everything from the watchdog's own
    ``max-load-15`` so a single source of truth controls both layers.

      * trip_15m  = max_load_15 - 0.5  — get out of the way 0.5 before
                                          the watchdog itself fires.
      * trip_5m   = max_load_15        — short-window can graze the
                                          long-window limit; we trip
                                          at that point even though the
                                          15-min hasn't caught up yet.
      * release_*  = max_load_15 - 1.0 — hysteresis margin to avoid
                                          flapping right at the trip
                                          boundary.

    With the stock ``max-load-15 = 6`` this returns
    ``(5.5, 6.0, 5.0, 5.0)`` — identical to the previously hard-coded
    values.
    """
    return (
        max_load_15 - 0.5,
        max_load_15,
        max_load_15 - 1.0,
        max_load_15 - 1.0,
    )


# Read once at import.  /etc/watchdog.conf changes require restarting
# the watchdog daemon to take effect anyway, so a service restart on
# our side is the natural moment to re-read.
#
# We intentionally do NOT log here — load_throttle is imported before
# the service has called setup_logging(), so an import-time log line
# would be silently dropped.  The first :class:`LoadThrottle` instance
# emits the same diagnostic from its constructor instead, by which
# point logging has been configured.
_MAX_LOAD_15 = _read_watchdog_max_load_15()
TRIP_15M, TRIP_5M, RELEASE_15M, RELEASE_5M = _derive_thresholds(_MAX_LOAD_15)


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
        # Logging is configured by the time the service actually
        # constructs us (unlike at module-import time, where the
        # derivation happens silently — see _MAX_LOAD_15 above).
        _logger.info(
            "load_throttle: thresholds derived from %s (max-load-15=%.1f): "
            "trip 15m>=%.1f or 5m>=%.1f, release 15m<%.1f and 5m<%.1f",
            _WATCHDOG_CONF, _MAX_LOAD_15,
            self._trip_15m, self._trip_5m,
            self._release_15m, self._release_5m,
        )

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
