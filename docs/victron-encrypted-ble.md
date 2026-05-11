# Victron Encrypted BLE (Instant Readout) Support

## Status: Paused — research complete, implementation deferred

## Problem

Victron products (SmartShunt, MPPT, Orion TR Smart, etc.) broadcast BLE
advertisements under manufacturer ID `0x02E1` using AES-CTR encrypted
payloads — the "Instant Readout" feature. Each device has a unique 32-hex-char
encryption key, viewable in VictronConnect under Product Info.

The current framework registers `0x02E1` for `BleDeviceVictronEnergy`, which
only handles **SolarSense 750** (unencrypted, specific byte pattern).
`check_manufacturer_data()` rejects encrypted payloads because bytes 0, 4, and 7
don't match the expected `0x10`/`0xff`/`0x01` SolarSense pattern. This produces
repeated errors in the Cerbo logs:

```
ERROR: {mac} - {name}: ignoring data b'...', manufacturer data check failed
```

These errors appear for every encrypted Victron device in BLE range, on every
advertisement cycle.

## Root Cause

`ble_device_victronenergy.py` line 19:

```python
def check_manufacturer_data(self, manufacturer_data: bytes) -> bool:
    if len(manufacturer_data) < 22 or manufacturer_data[0] != 0x10 \
       or manufacturer_data[4] != 0xff or manufacturer_data[7] != 0x01:
        return False
    return True
```

All Victron products share manufacturer ID `0x02E1`, but encrypted
advertisements have a different byte layout than SolarSense plaintext.
The handler assumes all `0x02E1` data is SolarSense and rejects everything else.

## Existing Solution (Separate Service)

The `dbus-victron-orion-tr` project handles this for Orion TR devices:

- Uses `dbus-ble-advertisements` as a BLE advertisement router (D-Bus signals)
- Decrypts using the `victron_ble` Python library
- Reads per-device keys from `config.ini` (MAC + KEY + INSTANCE)
- Publishes decrypted telemetry to D-Bus

Encryption keys for the user's devices are stored in
`home-secrets/deployments/dbus-victron-orion-tr/config.ini`.

## Proposed Integration

### Key Storage

Add a per-device D-Bus settings path:

```
/Settings/Devices/{dev_id}/EncryptionKey
```

This follows the existing pattern for `CustomName`, `Enabled`, and
`VrmInstance`. The key can be set via:

- SSH: `dbus -y com.victronenergy.settings /Settings/Devices/{dev_id}/EncryptionKey SetValue "..."`
- MQTT: write to the settings topic
- GUI: would require a new `ListTextField` on a per-device settings sub-page

### Framework Changes

1. **`ble_device_victronenergy.py`** — accept encrypted payloads in
   `check_manufacturer_data()`, attempt decryption in
   `handle_manufacturer_data()` when a key is configured, route by product ID.

2. **`dbus_ble_service.py`** — expose `/Devices/{dev_id}/EncryptionKey` as a
   proxy setting alongside Name and Enabled.

3. **Vendor `victron_ble`** — the decryption library, already used by
   `dbus-victron-orion-tr`, needs to be available in this project's `ext/`.

### GUI Changes (Optional, Deferred)

Currently `PageSettingsBleSensors.qml` shows a flat list with Name + Enabled
toggle per device. There is **no existing UI field** where a user could enter
an encryption key without GUI modifications.

To add key entry in the GUI:

- Change the repeater from `ListSwitch` to `ListNavigation` (per-device
  sub-page)
- Create `PageSettingsBleSensorDevice.qml` with:
  - `ListSwitch` for Enabled
  - `ListTextField` for CustomName
  - `ListTextField` for EncryptionKey (32-char hex, visible only for Victron
    devices)

This follows the pattern used by `PageDeviceInfo.qml` (`ListTextField` bound
to `/CustomName`) and `PageSettingsShelly.qml` (per-device sub-pages).

## Devices Affected

On the user's Cerbo GX, the following Victron devices broadcast encrypted
advertisements and are currently rejected:

| Name | Type | MAC |
|------|------|-----|
| Rooftop Solar | MPPT Solar Charger | f0:9d:2e:e9:a9:11 |
| Shunt Curb Side | SmartShunt | e3:83:1a:a8:f0:ad |
| 24v Mid Bay | Unknown (likely SmartShunt) | fb:8d:9f:a6:98:93 |
| Truck Connection | Orion TR Smart | ec:3b:5f:ac:52:ef |

## References

- `victron_ble` library: decryption of Victron Instant Readout advertisements
- `dbus-victron-orion-tr`: working reference implementation for Orion TR
- `home-secrets/services/dbus-victron-orion-tr/README.md`: key format docs
- Venus OS GUI: `gui-v2/pages/settings/PageSettingsBleSensors.qml`
- Venus OS wiki: `victron-wiki/dbus.md` section `## ble`
