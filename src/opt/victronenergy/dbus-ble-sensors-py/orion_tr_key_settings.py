"""
Persist Orion-TR BLE advertisement keys in ``com.victronenergy.settings``.

Paths are created with ``AddSilentSetting`` so they stay out of the normal
settings picker UI, but remain in the settings database for backup/restore.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from dbus_settings_service import DbusSettingsService

logger = logging.getLogger(__name__)


def _mac_key(dev_mac: str) -> str:
    """``dev_mac`` as used elsewhere in dbus-ble-sensors-py (12 hex chars, no colons)."""
    s = dev_mac.lower().replace(":", "")
    if not re.fullmatch(r"[0-9a-f]{12}", s):
        raise ValueError(f"invalid dev_mac: {dev_mac!r}")
    return s


def advertisement_key_setting_path(dev_mac: str) -> str:
    """Silent setting path for a device's 16-byte advertisement key.

    ``/Settings/Services/BleSensors`` is already registered as a leaf
    boolean (the global service enable switch); localsettings refuses to
    also register a GroupObject at that path, so we keep the Orion-TR
    keys under ``/Settings/Devices/orion_tr_<mac>/`` — the same tree the
    service already uses for per-device ``CustomName`` and ``Enabled``
    entries.
    """
    mk = _mac_key(dev_mac)
    return f"/Settings/Devices/orion_tr_{mk}/AdvertisementKey"


def get_advertisement_key(settings: DbusSettingsService, dev_mac: str) -> Optional[str]:
    path = advertisement_key_setting_path(dev_mac)
    raw = settings.try_get_value(path)
    if raw is None:
        return None
    s = str(raw).strip().lower().replace(" ", "")
    if len(s) != 32 or any(c not in "0123456789abcdef" for c in s):
        return None
    return s


def set_advertisement_key(settings: DbusSettingsService, dev_mac: str, key_hex: str) -> None:
    """Store 32-character hex key (16 bytes) into ``com.victronenergy.settings``.

    ``AddSilentSetting`` only seeds the *default* value of a path; if the
    setting already exists with a different current value (for example
    after a manual clear or a previously-persisted stale key), the add is
    a no-op on the live value.  We therefore ensure the setting exists
    and then write the live value with ``BusItem.SetValue``.
    """
    mk = _mac_key(dev_mac)
    s = str(key_hex).strip().lower().replace(" ", "")
    if len(s) != 32 or any(c not in "0123456789abcdef" for c in s):
        raise ValueError("key must be 32 hex characters")
    path = advertisement_key_setting_path(dev_mac)
    # Ensure the path exists (creates it on first run, seeds default).
    settings.set_item(path, s, 0, 0, silent=True)
    # Then push the actual value so a stale existing setting is replaced.
    settings.set_value(path, s)
    logger.info("Stored Orion-TR advertisement key for %s", mk)


def firmware_version_setting_path(dev_mac: str) -> str:
    """Silent setting path for the cached firmware version string."""
    return f"/Settings/Devices/orion_tr_{_mac_key(dev_mac)}/FirmwareVersion"


def get_firmware_version(settings: DbusSettingsService,
                        dev_mac: str) -> Optional[str]:
    path = firmware_version_setting_path(dev_mac)
    raw = settings.try_get_value(path)
    if raw is None:
        return None
    s = str(raw).strip()
    return s or None


def preferred_adapter_setting_path(dev_mac: str) -> str:
    return f"/Settings/Devices/orion_tr_{_mac_key(dev_mac)}/PreferredAdapter"


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
    """Store which BlueZ adapter (e.g. ``hci1``) last connected successfully."""
    mk = _mac_key(dev_mac)
    s = str(adapter).strip()
    if not s:
        return
    path = preferred_adapter_setting_path(dev_mac)
    settings.set_item(path, s, 0, 0, silent=True)
    settings.set_value(path, s)
    logger.info("Stored preferred adapter %s for Orion-TR %s", s, mk)


def set_firmware_version(settings: DbusSettingsService,
                         dev_mac: str, version: str) -> None:
    """Persist the firmware version string (free-form) in silent settings."""
    mk = _mac_key(dev_mac)
    s = str(version).strip()
    if not s:
        return
    path = firmware_version_setting_path(dev_mac)
    settings.set_item(path, s, 0, 0, silent=True)
    settings.set_value(path, s)
    logger.info("Stored Orion-TR firmware version %r for %s", s, mk)
