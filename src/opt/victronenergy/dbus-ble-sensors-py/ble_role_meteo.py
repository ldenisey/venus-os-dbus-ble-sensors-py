from ble_role import BleRole


class BleRoleMeteo(BleRole):
    """
    Meteo sensor role class.

    Device claiming this role can provide 'Irradiance', 'CellTemperature', 'ExternalTemperature', 'ExternalTemperature2'
    'WindSpeed', 'WindDirection', 'InstallationPower' and/or 'TodaysYield' items. Settings can be 'WindSpeedSensor' and
    'ExternalTemperatureSensor'

    Cf.:
    - https://github.com/victronenergy/gui-v2/blob/main/pages/settings/devicelist/PageMeteo.qml
    - https://github.com/victronenergy/gui-v2/blob/main/pages/settings/devicelist/PageMeteoSettings.qml
    """

    NAME = 'meteo'

    def __init__(self, config: dict = None):
        super().__init__()

        self.info.update(
            {
                'name': 'meteo',
                'dev_instance': 20,
                'settings': [],
            },
        )
