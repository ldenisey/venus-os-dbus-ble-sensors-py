# SeeLevel 709-BT integration (`dbus-ble-sensors-py`)

Status: **integrated.**  Both BTP3 (Cypress, MFG ID `0x0131`) and BTP7
(SeeLevel, MFG ID `0x0CC0`) are published as standard
`com.victronenergy.tank.*`, `com.victronenergy.temperature.*`, and
`com.victronenergy.battery.*` D-Bus services through the existing
device-class autoloader.  Sensors appear in the Cerbo *Settings →
Integrations → Bluetooth Sensors* UI alongside other supported BLE
devices; no separate UI plumbing.

## Architecture overview

The Garnet 709-BT operates as a BLE Broadcaster: it cycles through its
attached sensors and emits an advertisement packet for each, with the
sensor-specific payload in the manufacturer-specific data field.  No
BLE connection or GATT exchange is required.  The entire integration
is read-only consumption of advertisement frames.

This makes the SeeLevel driver substantially simpler than the IP22 or
Orion-TR drivers:

- No GATT write path
- No DVCC contract participation
- No key provisioning or pairing
- No external-control state machine
- One advertisement → one sensor data update

The driver parses each frame into the appropriate role
(`tank` / `temperature` / `battery`) and lets the framework register
the corresponding `com.victronenergy.<role>` D-Bus service and emit
its values.  Tank levels feed `/Level`, temperature into `/Temperature`,
battery voltage into `/Dc/0/Voltage`.

### Per-MAC sensor multiplexing

A single 709-BT broadcasts on **one MAC address** but reports many
sensors over time, with different sensor numbers in successive
advertisements.  Each sensor becomes its own D-Bus service with a
unique role-and-instance combination, all attributable to the same
underlying physical hardware (identified by Coach ID in bytes 0-2).
Services are created lazily as new sensor numbers appear, so a freshly
deployed 709-BT will populate the device list incrementally over the
first cycle.

## Files

- [`seelevel_common.py`](../src/opt/victronenergy/dbus-ble-sensors-py/seelevel_common.py):
  `BleDeviceSeeLevel` base class with the shared protocol reference
  in the module docstring, the status-code table, and the tank/
  temperature/battery service-construction helpers.
- [`ble_device_seelevel_btp3.py`](../src/opt/victronenergy/dbus-ble-sensors-py/ble_device_seelevel_btp3.py):
  `BleDeviceSeeLevelBTP3`, manufacturer ID `0x0131` (Cypress).
- [`ble_device_seelevel_btp7.py`](../src/opt/victronenergy/dbus-ble-sensors-py/ble_device_seelevel_btp7.py):
  `BleDeviceSeeLevelBTP7`, manufacturer ID `0x0CC0` (SeeLevel).
- [`tests/test_ble_device_seelevel.py`](../src/opt/victronenergy/dbus-ble-sensors-py/tests/test_ble_device_seelevel.py):
  captured-payload fixtures and assertions for both protocols.

## Advertisement payload

Both variants use 14 bytes of manufacturer-specific data with a
common 3-byte coach-ID header.

### BTP3 (`0x0131`, Cypress)

| Bytes | Field |
|---|---|
| 0-2 | Coach ID (24-bit, little-endian) |
| 3 | Sensor number (0-13) |
| 4-6 | Sensor data (3 ASCII chars: `OPN`, `ERR`, or numeric) |
| 7-9 | Volume in gallons (3 ASCII chars) |
| 10-12 | Total capacity in gallons (3 ASCII chars) |
| 13 | Alarm state (ASCII digit '0'-'9') |

Each frame reports **one** sensor.  The sensor number in byte 3 routes
to the corresponding role.

### BTP7 (`0x0CC0`, SeeLevel)

| Bytes | Field |
|---|---|
| 0-2 | Coach ID (24-bit, little-endian) |
| 3-10 | Tank levels, 1 byte each (0-100 = %, >100 = error code) |
| 11 | Battery voltage × 10 (e.g. 130 = 13.0 V) |
| 12-13 | Unused |

Tank slot order in bytes 3-10:
**Fresh, Wash, Toilet, Fresh2, Wash2, Toilet2, Wash3, LPG**.

Each frame reports **all** tanks plus battery voltage at once, so a
single advertisement triggers updates across multiple D-Bus services.

## Sensor numbers

| # | BTP3 (`0x0131`) | BTP7 (`0x0CC0`) |
|---|---|---|
| 0 | Fresh Water | Fresh Water |
| 1 | Toilet Water | Wash Water |
| 2 | Wash Water | Toilet Water |
| 3 | LPG | Fresh Water 2 |
| 4 | LPG 2 | Wash Water 2 |
| 5 | Galley Water | Toilet Water 2 |
| 6 | Galley Water 2 | Wash Water 3 |
| 7 | Temperature | LPG |
| 8 | Temperature 2 | Battery (voltage × 10) |
| 9 | Temperature 3 | (unused) |
| 10 | Temperature 4 | (unused) |
| 11 | Chemical | (unused) |
| 12 | Chemical 2 | (unused) |
| 13 | Battery (voltage × 10) | (unused) |

The two protocols share the same hardware family but **do not share
sensor-number-to-type mapping**.  The driver picks the correct table
based on the manufacturer ID of the incoming frame.

## Status / error codes

### BTP3 sentinel strings (bytes 4-6)

| Sentinel | Meaning |
|---|---|
| `OPN` | Sensor open / disconnected.  No D-Bus service created for this slot. |
| `ERR` | Sensor error.  Service is created but published with error status. |
| numeric | Actual reading. |

### BTP7 tank-byte error codes (>100)

| Code | Meaning |
|---|---|
| 101 | Short Circuit |
| 102 | Open / No response |
| 103 | Bitcount error |
| 104 | Non-stacked configured but stacked data received |
| 105 | Stacked, missing bottom-sender data |
| 106 | Stacked, missing top-sender data |
| 108 | Bad Checksum |
| 110 | Tank disabled |
| 111 | Tank init |

`BleDeviceSeeLevel.STATUS_CODES` exposes these as a Python dict for
in-driver lookup and error-message formatting.

## Unit conversions

| From | To | Factor |
|---|---|---|
| Tank level | percentage (0-100) | direct |
| Temperature (°F) | °C | `(F − 32) × 5/9` |
| Battery voltage byte | volts | `byte / 10` |
| Tank capacity (gallons) | m³ | `× 0.00378541` |

Capacity conversion only applies if a user sets a non-zero capacity on
the tank service; the protocol itself reports level only, not absolute
volume.

## Victron D-Bus mappings

### Product IDs

| Role | Product ID | Constant |
|---|---|---|
| Tank Sensor | `0xA142` | `VE_PROD_ID_TANK_SENSOR` |
| Temperature Sensor | `0xA143` | `VE_PROD_ID_TEMPERATURE_SENSOR` |
| Battery Monitor | `0xA381` | `VE_PROD_ID_BATTERY_MONITOR` |

### Fluid types

| Sensor name | FluidType | Constant |
|---|---|---|
| Fresh Water | `1` | `FLUID_TYPE_FRESH_WATER` |
| Wash Water | `2` | `FLUID_TYPE_WASTE_WATER` |
| Toilet Water | `5` | `FLUID_TYPE_BLACK_WATER` |
| LPG | `8` | `FLUID_TYPE_LPG` |
| Chemical | `0` | (custom) |

> **Note:** "Toilet Water" tanks are mapped to `FLUID_TYPE_BLACK_WATER`
> (5) for Victron compatibility, even though the UI label is "Toilet
> Water" rather than "Black Water".

## Migration from `victron-seelevel-python`

The standalone [TechBlueprints/victron-seelevel-python](https://github.com/TechBlueprints/victron-seelevel-python)
service is **superseded** by this integration.  Differences a migrator
should know about:

| Aspect | Standalone | This integration |
|---|---|---|
| Scanning | Required `dbus-ble-advertisements` router process | Built-in, via `dbus-ble-sensors-py`'s HCI tap |
| UI | Custom `/SwitchableOutput/relay_*` switches under a dedicated SeeLevel device entry | Standard *Settings → Integrations → Bluetooth Sensors* UI |
| Per-sensor enable/disable | Custom switch per sensor | Per-MAC enable in the Bluetooth Sensors menu |
| CPU usage | `btmon` parsing in Python loop | HCI monitor channel via raw socket; ~order of magnitude lower |
| Service architecture | Two services (router + SeeLevel) | One in-process driver |

To migrate, **disable both standalone services first** so they don't
contend for bus names or scanning:

```sh
svc -d /service/dbus-ble-advertisements
svc -d /service/seelevel-python   # actual path may vary by install
```

Then install this build:

```sh
curl -fsSL https://raw.githubusercontent.com/TechBlueprints/venus-os-dbus-ble-sensors-py/main/install.sh | bash
```

Settings carried by the standalone (custom names, fluid-type
overrides, capacities) **do not migrate automatically**.  They are
re-entered through the standard Bluetooth Sensors UI on first
discovery.

## Attribution

BTP7 advertisement-format decoding (manufacturer ID `0x0CC0`, byte
layout, error codes 101 to 111) was originally contributed by Andreas
Tillack ([@atillack](https://github.com/atillack)) at
[atillack/victron-seelevel-python](https://github.com/atillack/victron-seelevel-python)
and merged upstream as
[PR #2](https://github.com/TechBlueprints/victron-seelevel-python/pull/2)
of `TechBlueprints/victron-seelevel-python`.

## References

- Protocol decode discussion: https://github.com/custom-components/ble_monitor/issues/1181
- BTP7 btmon capture: https://github.com/TechBlueprints/victron-seelevel-python/issues/1
- BTP7 PR discussion: https://github.com/TechBlueprints/victron-seelevel-python/pull/2
- Standalone implementation (deprecated): https://github.com/TechBlueprints/victron-seelevel-python
