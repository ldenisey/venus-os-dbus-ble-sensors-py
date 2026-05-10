# Orion-TR Smart integration (`dbus-ble-sensors-py`)

This document describes the Orion-TR Smart driver integrated into
`venus-os-dbus-ble-sensors-py`.  It handles encrypted BLE advertisements,
automatic key provisioning, GATT mode-write (on/off), and daily firmware
refresh — all within the existing scan-loop architecture.

## Architecture overview

```
BLE advertisement (0x02E1, product 0xA3C0–0xA3DF)
        │
        ▼
 dbus_ble_sensors.py  ──dispatch──▶  BleDeviceOrionTR
        │                               │
        │  bleak scanner (hci0/hci2)    ├── victron_ble decrypt + parse
        │  paused during GATT bursts    ├── dcdc / alternator role flip
        │  via scan_control.py          ├── /Mode GATT write
        │                               └── key / firmware provisioning
        │                                       │
        ▼                                       ▼
 com.victronenergy.dcdc.*             orion_tr_key_cli.py (subprocess)
 or com.victronenergy.alternator.*    ├── PUK CRC auth (97580006)
 (gui-v2 PageDcDcConverter.qml)       ├── Subscribe 0xEDDB (prime)
                                      ├── GetValue 0xEC65 (key)
                                      ├── GetValue 0x0140 (firmware)
                                      ├── GetValue 0xEDDB (temperature)
                                      └── GetValue 0x0100 (product id)
```

### Key design decisions

1. **Subprocess provisioning.**  PUK authentication and VREG reads run in
   `orion_tr_key_cli.py` as a separate Python process.  This isolates the
   GATT session from the long-running service's dbus-python / BlueZ state,
   which otherwise causes corrupt CCCD writes on reconnection (upper byte
   of the CCCD value drifts into the `0x70–0x79` range after the first
   cycle, silently disabling device notifications).

2. **`victron_ble` for decryption.**  The upstream `victron_ble` library
   is vendored under `ext/victron_ble/` with a one-line patch to its
   `base.py` so it uses `cryptography` (shipped in Venus OS) instead of
   PyCryptodome (not available).

3. **Scan pause/resume.**  `scan_control.pause_scanning()` /
   `resume_scanning()` are ref-counted hooks that make the `BleakScanner`
   yield the adapter during GATT bursts.  The scan loop polls
   `is_scanning_paused()` every 250 ms inside its active-scan context so
   it exits within one BLE advertising interval.

4. **Multi-adapter support.**  All GATT paths resolve the device across
   every BlueZ adapter via `ObjectManager` rather than hard-coding `hci0`.

## Files

| File | Purpose |
|------|---------|
| `ble_device_orion_tr.py` | Device driver: dispatch, decode, dcdc↔alternator flip, /Mode write, provision lifecycle, daily refresh |
| `orion_tr_key_cli.py` | Standalone subprocess: PUK auth + VREG reads for key, firmware, temperature, product id |
| `orion_tr_gatt.py` | Async GATT register writer for /Mode (paired, encrypted CBOR SetValue) |
| `orion_tr_key_settings.py` | Silent-setting helpers for `AdvertisementKey` and `FirmwareVersion` |
| `orion_tr_pin.py` | Pairing passkey resolution: ini → Cerbo setting → default 0 |
| `ble_role_dcdc.py` | `dcdc` role (D-Bus paths for `PageDcDcConverter.qml`) |
| `ble_role_alternator.py` | `alternator` role (D-Bus paths for `PageAlternator.qml`) |
| `scan_control.py` | Ref-counted pause/resume for the BleakScanner loop |
| `ext/victron_ble/` | Vendored + patched `victron_ble` library |

## D-Bus service paths

Service name: `com.victronenergy.dcdc.orion_tr_<mac12>` (or `.alternator.`
when the unit is in a charger algorithm).

| Path | Source | Notes |
|------|--------|-------|
| `/ProductId` | Advertisement bytes 2–3 | Verified against VREG 0x0100 |
| `/ProductName` | BlueZ `Device1.Name` | BLE-advertised name (e.g. "Orion Smart HQ20326VVVJ") |
| `/FirmwareVersion` | VREG 0x0140 via CLI | Decoded as BCD "major.minor" (e.g. "1.10") |
| `/Dc/In/V` | Advertisement | 0.01 V units |
| `/Dc/0/Voltage` | Advertisement | 0.01 V units |
| `/Dc/0/Temperature` | VREG 0xEDDB via CLI | Celsius (0.01 K raw → subtract 273.15). Updated daily. |
| `/State` | Advertisement | VE.Direct OperationMode enum |
| `/ErrorCode` | Advertisement | 0 = no error |
| `/DeviceOffReason` | Advertisement | Bitmask |
| `/Mode` | Writable (1=On, 4=Off) | Triggers GATT SetValue on VREG 0x0200 |
| `/Connected` | Always 1 when adverts decode | |

Paths that are published but currently invalid (hidden by gui-v2's
`preferredVisible: dataItem.valid`): `/Dc/In/I`, `/Dc/In/P`,
`/Dc/0/Current`, `/Dc/0/Power`.  These are not available from the
advertisement payload.  The standalone driver also leaves them blank.

## Configuration

### Advertisement key (automatic)

On first advertisement from an Orion-TR that has no key cached, the
driver pauses scanning and spawns `orion_tr_key_cli.py` to:
1. Pair using the GX passkey (from `orion_tr_pin.py`)
2. PUK CRC authenticate on `97580006`
3. Read VREG `0xEC65` (16-byte key)
4. Read VREG `0x0140` (firmware), `0xEDDB` (temperature), `0x0100` (product id)

The key is stored as a silent setting at:
`/Settings/Devices/orion_tr_<mac12>/AdvertisementKey`

If decryption later fails with `AdvertisementKeyMismatchError`, the
driver clears the in-memory key and re-provisions (with 3-minute backoff).

### Pairing PIN

Resolution order (highest priority first):
1. `/data/conf/dbus-ble-sensors-py-orion.ini` `[orion] PairingPin`
2. `/Settings/Ble/Service/Pincode` (Cerbo Bluetooth PIN from gui-v2)
3. Default `0` (passkey `000000`)

### Daily refresh

Between 3 AM and 5 AM local time, the first advertisement received from
a provisioned Orion-TR triggers a GATT session that re-reads firmware
and temperature.  Runs at most once per calendar day per device, only
when the device is in range (sending advertisements).

## Stock service conflict

Venus OS ships `dbus-ble-sensors` (C binary) which claims the same
`com.victronenergy.ble` bus name.  The start script
(`start-dbus-ble-sensors-py.sh`) automatically stops it with
`svc -d /service/dbus-ble-sensors` before launching.

## Build / deployment

### Development (scp)

```bash
scp src/opt/victronenergy/dbus-ble-sensors-py/*.py \
    root@dev-cerbo:/opt/victronenergy/dbus-ble-sensors-py/
scp -r src/opt/victronenergy/dbus-ble-sensors-py/ext/victron_ble \
    root@dev-cerbo:/opt/victronenergy/dbus-ble-sensors-py/ext/
ssh root@dev-cerbo svc -t /service/dbus-ble-sensors-py
```

### opkg build

`requirements.sh` installs `victron-ble` from PyPI and patches it for
`cryptography`.  The patch is a sed replacement of the `Crypto.Cipher`
imports in `ext/victron_ble/devices/base.py`.

### Python dependency

`python3-cryptography` (opkg) — shipped in the stock Venus OS image.
No PyCryptodome needed.
