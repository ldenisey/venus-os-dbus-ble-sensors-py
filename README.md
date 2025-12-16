# Dbus BLE sensors py

Venus OS dbus service for BLE device support. Replaces and extends [official dbus ble service](https://github.com/victronenergy/dbus-ble-sensors/tree/master) which does not allow collaboration for new devices support.

Devices currently supported :
| Brand          | model                      | Product page                                                                                                   |
| -------------- | -------------------------- | -------------------------------------------------------------------------------------------------------------- |
| Teltonika      | EYE Sensor                 | https://www.teltonika-gps.com/products/accessories/sensors-beacons/eye-sensor-standard                         |
| Safiery        | Star Tank                  | https://safiery.com/product/tank-level-sensor-star-tank-phased-coherent-radar-battery/                         |
| Gobius         | Gobius C                   | https://gobiusc.com/fr/                                                                                        |
| Victron Energy | SolarSense 750             | https://www.victronenergy.com/upload/documents/Datasheet-SolarSense-750-EN.pdf                                 |
| Mopeka         | Mopeka Pro Check Universal | https://mopeka.com/consumer-solutions/#:~:text=Mopeka%20Pro%20Check%20Universal%20%E2%80%93%20Latest%20Version |
| Mopeka         | Mopeka Pro Check H2O       | https://mopeka.com/commercial-industry-based-solutions/water/#:~:text=Mopeka%20Pro%20Check                     |
| Mopeka         | Mopeka Pro Check LPG       | https://mopeka.com/consumer-solutions/#:~:text=Mopeka%20Pro%20Check,-Ideal%20for%20Residential                 |
| Mopeka         | Mopeka Pro 200             | https://mopeka.com/consumer-solutions/#:~:text=Mopeka%20Pro200                                                 |
| Mopeka         | Mopeka Pro Plus            | https://mopeka.com/consumer-solutions/#:~:text=Mopeka%20Pro%20Plus                                             |
| Mopeka         | Mopeka TD40                | https://mopeka.com/consumer-solutions/#:~:text=Mopeka%20TD40                                                   |
| Mopeka         | Mopeka TD200               | https://mopeka.com/commercial-industry-based-solutions/water/#:~:text=Mopeka%20TD40,%20TD200                   |
| Ruuvi          | Ruuvi Tag                  | https://ruuvi.com/ruuvitag/                                                                                    |
| Ruuvi          | Ruuvi Air                  | https://ruuvi.com/air/                                                                                         |

## Installation

Add the [venus-os-configuration opkg feed](https://github.com/ldenisey/venus-os-configuration/blob/main/docs/VenusOS-Opkg_configuration.md#adding-custom-feed), then :
``` bash
opkg install dbus-ble-sensors-py
```

To make it persistent to Venus OS firmware upgrades, you can [install mod-persist](https://github.com/ldenisey/venus-os-mod-persist/tree/main?tab=readme-ov-file#installation) then :
``` bash
persist-opkg install dbus-ble-sensors-py
```

## Usage

Device scan and enabling is done through the GUI, as described in the official documentations, i.e. [Cerbo GX bluetooth](https://www.victronenergy.com/media/pg/Cerbo_GX/en/connecting-supported-non-victron-products.html#UUID-8def9c4a-f36e-7048-1b4f-7294538eb31b).  
In short devices can be enabled/disabled in *Settings* -> *Integrations* -> *Bluetooth Sensors* and configured in *Settings* -> *Devices* dedicated menu.

> [!NOTE]  
> Even though the configuration process is the same, the configuration themselves are NOT shared between this service and official ble service
> hence configuration will have to be reset when switching between the two.

## Development

For technical info and guide to add new devices, see [dedicated developer page](DEVELOPMENT.md).
