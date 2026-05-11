"""
Persist Blue Smart IP22 BLE advertisement keys in
``com.victronenergy.settings``.

Kept in a separate namespace from the Orion-TR keys so each product
family stays self-contained in the settings tree.  Paths live under
``/Settings/Devices/ip22_<mac>/`` alongside the standard per-device
``CustomName`` / ``Enabled`` entries.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from dbus_settings_service import DbusSettingsService

logger = logging.getLogger(__name__)

def _mac_key(dev_mac: str) -> str:
    s = dev_mac.lower().replace(":", "")
    if not re.fullmatch(r"[0-9a-f]{12}", s):
        raise ValueError(f"invalid dev_mac: {dev_mac!r}")
    return s

def advertisement_key_setting_path(dev_mac: str) -> str:
    return f"/Settings/Devices/ip22_{_mac_key(dev_mac)}/AdvertisementKey"

def get_advertisement_key(settings: DbusSettingsService,
                          dev_mac: str) -> Optional[str]:
    path = advertisement_key_setting_path(dev_mac)
    raw = settings.try_get_value(path)
    if raw is None:
        return None
    s = str(raw).strip().lower().replace(" ", "")
    if len(s) != 32 or any(c not in "0123456789abcdef" for c in s):
        return None
    return s

def set_advertisement_key(settings: DbusSettingsService,
                          dev_mac: str, key_hex: str) -> None:
    mk = _mac_key(dev_mac)
    s = str(key_hex).strip().lower().replace(" ", "")
    if len(s) != 32 or any(c not in "0123456789abcdef" for c in s):
        raise ValueError("key must be 32 hex characters")
    path = advertisement_key_setting_path(dev_mac)
    settings.set_item(path, s, 0, 0, silent=True)
    settings.set_value(path, s)
    logger.info("Stored IP22 advertisement key for %s", mk)

def firmware_version_setting_path(dev_mac: str) -> str:
    return f"/Settings/Devices/ip22_{_mac_key(dev_mac)}/FirmwareVersion"

def get_firmware_version(settings: DbusSettingsService,
                         dev_mac: str) -> Optional[str]:
    path = firmware_version_setting_path(dev_mac)
    raw = settings.try_get_value(path)
    if raw is None:
        return None
    s = str(raw).strip()
    return s or None

def set_firmware_version(settings: DbusSettingsService,
                         dev_mac: str, version: str) -> None:
    mk = _mac_key(dev_mac)
    s = str(version).strip()
    if not s:
        return
    path = firmware_version_setting_path(dev_mac)
    settings.set_item(path, s, 0, 0, silent=True)
    settings.set_value(path, s)
    logger.info("Stored IP22 firmware version %r for %s", s, mk)

def preferred_adapter_setting_path(dev_mac: str) -> str:
    return f"/Settings/Devices/ip22_{_mac_key(dev_mac)}/PreferredAdapter"

def get_preferred_adapter(settings: DbusSettingsService,
                          dev_mac: str) -> Optional[str]:
    path = preferred_adapter_setting_path(dev_mac)
    raw = settings.try_get_value(path)
    if raw is None:
        return None
    s = str(raw).strip()
    return s or None

def set_preferred_adapter(settings: DbusSettingsService,
                          dev_mac: str, adapter: str) -> None:
    mk = _mac_key(dev_mac)
    s = str(adapter).strip()
    if not s:
        return
    path = preferred_adapter_setting_path(dev_mac)
    settings.set_item(path, s, 0, 0, silent=True)
    settings.set_value(path, s)
    logger.info("Stored preferred adapter %s for IP22 %s", s, mk)
