"""
Resolve BLE pairing passkey for Orion-TR SMP / Victron pairing agent.

Order (highest priority first):
1. Optional ini: ``/data/conf/dbus-ble-sensors-py-orion.ini`` ``[orion] PairingPin``
2. Cerbo GX setting: ``/Settings/Ble/Service/Pincode`` (see gui-v2 ``PageSettingsBluetooth.qml``)
3. Default ``0`` (six-digit passkey ``000000`` in the BlueZ agent UI sense)
"""
from __future__ import annotations

import configparser
import logging
import os
import re
from typing import Optional

from dbus_settings_service import DbusSettingsService

from conf import ORION_OPTIONAL_INI

logger = logging.getLogger(__name__)

CERBO_BLE_SERVICE_PINCODE = "/Settings/Ble/Service/Pincode"


def _parse_ini_pin() -> Optional[int]:
    path = ORION_OPTIONAL_INI
    if not path or not os.path.isfile(path):
        return None
    try:
        cfg = configparser.ConfigParser()
        cfg.read(path)
        if not cfg.has_section("orion"):
            return None
        raw = cfg.get("orion", "PairingPin", fallback="").strip()
        if not raw:
            return None
        return int(re.sub(r"\D", "", raw))
    except Exception:
        logger.exception("Failed to read optional Orion pairing pin from %r", path)
        return None


def _coerce_pin_value(raw) -> Optional[int]:
    if raw is None:
        return None
    if isinstance(raw, (int,)):
        return int(raw)
    s = str(raw).strip()
    if not s:
        return None
    digits = re.sub(r"\D", "", s)
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def resolve_pairing_passkey(settings: DbusSettingsService) -> int:
    override = _parse_ini_pin()
    if override is not None:
        logger.debug("Orion pairing passkey: from %r", ORION_OPTIONAL_INI)
        return override

    cerbo = settings.try_get_value(CERBO_BLE_SERVICE_PINCODE)
    pin = _coerce_pin_value(cerbo)
    if pin is not None:
        logger.debug("Orion pairing passkey: from Cerbo %r", CERBO_BLE_SERVICE_PINCODE)
        return pin

    logger.debug("Orion pairing passkey: default 0 (000000)")
    return 0
