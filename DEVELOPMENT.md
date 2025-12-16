# Developer guide

## Local execution

After cloning, execute script [requirements.sh](./src/CONTROL/requirements.sh) to download third party libraries.

To execute locally on a Venus OS device, stop existing *dbus-ble-sensors* service with command `svc -d /service/dbus-ble-sensors`
and execute `python3 dbus-ble-sensors-py` (parameter `-d` activates debug traces).

## Tests

Tests are to be placed in [test folder](./src/opt/victronenergy/dbus-ble-sensors-py/tests/).
They can be executed with `python3 -m unittest -v` or selectively with `python3 -m unittest <test class>`.

## Project architecture

[dbus_ble_sensors.py](./src/opt/victronenergy/dbus-ble-sensors-py/dbus_ble_sensors.py) is the entry pont,
reponsible for listing bluetooth adapters, running scans, filtering and redirecting advertising frames 
to the responsible device class.

[ble_role.py](./src/opt/victronenergy/dbus-ble-sensors-py/ble_role.py) and it subclasses `ble_role_*.py` define base
features (data) that a device can provide: `temperature`, `tank`, `meteo`, `digitalinput` and `movement`.
One device can have multiple roles, i.e. a temperature sensor can also provide movement or magnet input data.
Role classes can also define common settings (mostly those used by the GUI) and alarms.

[ble_device.py](./src/opt/victronenergy/dbus-ble-sensors-py/ble_device.py) and its subclasses `ble_device_*.py` are
device definition classes. There can only be one class per manufacturer but one class can manage several devices
of the same manufacturer. Beside identification information, it contains bluetooth manufacturer data parsing rules
and optionally settings and alarms.

[dbus_settings_service](./src/opt/victronenergy/dbus-ble-sensors-py/dbus_settings_service.py) reads/writes settings from
*com.vistronenergy.settings* dbus service, itself responsible of storing those on disk for persistence.

[dbus_ble_service](./src/opt/victronenergy/dbus-ble-sensors-py/dbus_ble_service.py) publishes the *com.victronenergy.ble*
service expected by the *Settings* -> *Integrations* -> *Bluetooth Sensors* UI  menu to expose configuration
and bluetooth devices.

[dbus-role-service](./src/opt/victronenergy/dbus-ble-sensors-py/dbus_role_service.py) publishes one specific dbus service
for every device's role. Those will expose the parsed data to the UI and to other services.

## Adding a new device

### Device file

To add support to a new device, add `ble_device_<vendor>.py` under `src/opt/victronenergy/dbus-ble-sensors-py/`, it will be automatically detected.

This class must :
- subclass `BleDevice`
- define a static `MANUFACTURER_ID`, corresponding to the [bluetooth manufacturer code](https://bitbucket.org/bluetooth-SIG/public/src/main/assigned_numbers/company_identifiers/company_identifiers.yaml) of the device
- implement method `configure(manufacturer_data: bytes)` to specify the [device info fields](#device-info-fields) using `self.info.update({...})` or alike

This class can :
- implement `check_manufacturer_data(bytes) -> bool` which is called for quick manufacturer data frame check before parsing, for example on data length and/or predefined bytes.
- implement `update_data(role_service, sensor_data)` which is called after manufacturer data parsing but before they are published in dbus, it can be used for any data transformation that can not be done with parsing regs.
- host device parsing *xlate*, alarm *update* and setting *onchange* needed methods.

#### Device info fields

| Name               | Occurrence | Type         | Description                                                 |
| ------------------ | ---------- | ------------ | ----------------------------------------------------------- |
| `product_id`       | Mandatory  | `int`        | custom product identifier                                   |
| `product_name`     | Mandatory  | `str`        | general product name (e.g., 'Mopeka sensor')                |
| `device_name`      | Mandatory  | `str`        | default UI device name (e.g. 'Mopeka LPG')                  |
| `dev_prefix`       | Mandatory  | `str`        | short, no spaces, used to build service paths and device id |
| `hardware_version` | Optional   | `str`        | device hardware version                                     |
| `firmware_version` | Optional   | `str`        | device firmware version                                     |
| `roles`            | Mandatory  | `dict`       | keys are role names, values are role-specific dict config   |
| `regs`             | Mandatory  | `list[dict]` | byte [parsing rules](#parsing-rules) list                   |
| `settings`         | Optional   | `list[dict]` | list of [settings](#settings)                               |
| `alarms`           | Optional   | `list[dict]` | list of [alarms](#alarms)                                   |

#### Parsing rules

Each reg dictionary define how to extract one value from sensor manufacturer data bytes. It is composed of :

| Name     | Occurrence | Type                | Description                                                                                                        |
| -------- | ---------- | ------------------- | ------------------------------------------------------------------------------------------------------------------ |
| `name`   | Mandatory  | `str`               | key published on the role service (e.g., `BatteryVoltage`, `Irradiance`)                                           |
| `type`   | Mandatory  | `VeDataBasicType`   | cf [ve_types.py](./src/opt/victronenergy/dbus-ble-sensors-py/ve_types.py)                                          |
| `roles`  | Optional   | `list[str or None]` | device role(s) the data is relevant for, all roles by default, if contains `None`, data will be ignored completely |
| `offset` | Mandatory  | `int`               | byte index in manufacturer data                                                                                    |
| `shift`  | Optional   | `int`               | right bit shift to apply to raw data                                                                               |
| `bits`   | Depends    | `int`               | mandatory for `VE_HEAP_STR` type else default value is based on type field                                         |
| `scale`  | Optional   | `int`               | value to divide the raw data with                                                                                  |
| `bias`   | Optional   | `int`               | value to add to the raw data with                                                                                  |
| `xlate`  | Optional   | callable            | custom method to modify the raw data                                                                               |
| `flags`  | Optional   | `list[str]`         | list of `REG_FLAG_INVALID` (enables `inval`) or `REG_FLAG_BIG_ENDIAN` (read bytes as big-endian)                     |
| `inval`  | Optional   | `int`               | if `REG_FLAG_INVALID` flag is set, sentinel value marking the value invalid (`None`)                               |

> [!NOTE]  
> Computation are done in this order: extract `type` or `bits` length at `offset` position, `shift`, `bits` mask,
> two’s complement conversion depending of `type`, `scale`, `bias`, `xlate`, `inval`

> [!NOTE]  
> `xlate` can only access the raw data it is defined for. If your custom computation requires other parsed data or settings,
> override `update_data` method instead.

> [!NOTE]  
> `VE_HEAP_STR` (string value) requires `bits` divisible by 8; raw value is NUL-stripped and decoded as UTF-8.


#### Settings

Settings dicts define values persisted on disk through dbus *com.victronenergy.ble* service.
They can be defined by devices and roles. Most are created for interacting with users through the UI.

They are defined with a dict containing :
- `name` string in PascalCase, without space or special chars except `/` separators for hierarchical organization
- `props` dict containing :
  - `type`: one of `VeDataBasicType`, cf [ve_types.py](./src/opt/victronenergy/dbus-ble-sensors-py/ve_types.py)
  - `def`: default value for first time setting initialization
  - `min`: minimal value, only if def is a number
  - `max`: maximal value, only if def is a number

#### Alarms

Alarms dicts define warning and/or alarm triggers predefined in Venus OS depending on the device data.
They will trigger a notification in the UI, alarm state will also make the device beep.

They are defined with a dict containing :
- `name` string in PascalCase, without space or special chars except `/` separators for hierarchical organization
- `update` callable returning alarm new state based on latest data: 0 (none), 1 (warning), 2 (alarm).

> [!NOTE]  
> Warning/alarm levels are not consistent through the different roles and new alarms can not be added to the predefined ones.
