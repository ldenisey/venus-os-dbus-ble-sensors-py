"""
Shared pause/resume hooks for the ``BleakScanner`` main loop.

Some drivers (currently the Orion-TR integration) need exclusive GATT
access to ``hci0`` for pairing, notifications and register reads.  Leaving
``bleak.BleakScanner`` running at the same time triggers
``org.bluez.Error.InProgress`` errors from BlueZ and starves out the
``PropertiesChanged`` signals that we subscribe to.

The orchestration is intentionally tiny: a reference-counted pause flag
that the scan loop polls between iterations.  Drivers wrap their GATT
bursts in :func:`pause_scanning` / :func:`resume_scanning` calls.  The
scan loop in :mod:`dbus_ble_sensors` checks :func:`is_scanning_paused`
between iterations and idles instead of starting a fresh scanner while
the flag is set.

Lives in its own module to avoid an import cycle between
``dbus_ble_sensors`` and driver modules that pause/resume scanning.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_refs = 0


def pause_scanning(reason: str = "") -> None:
    global _refs
    _refs += 1
    if _refs == 1:
        logger.info("BLE scan loop paused (%s)", reason or "unspecified")


def resume_scanning(reason: str = "") -> None:
    global _refs
    if _refs <= 0:
        logger.warning("resume_scanning called while not paused")
        _refs = 0
        return
    _refs -= 1
    if _refs == 0:
        logger.info("BLE scan loop resumed (%s)", reason or "unspecified")


def is_scanning_paused() -> bool:
    return _refs > 0
