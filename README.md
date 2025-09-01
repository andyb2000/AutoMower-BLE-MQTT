# AutoMower-BLE-MQTT
Python script for AutoMower-BLE for Husqvarna mowers to send status to MQTT and allow mow and dock commands

# Purpose/Information
Due to the complexity of integrations for Home Assistant (HASS) and the constant changes to the ESPHome Bluetooth proxy, I created this simple script that can be ran on a Raspberry PI (Bluetooth models) that will communicate with your mower, connect to your MQTT server and publish status information. It will also subscribe to two topics that will allow for MOW and PARK.

# Compatibility
This program uses the excellent library by @alistair23 at https://github.com/alistair23/AutoMower-BLE/ so this is a program requirement.
All mowers supported by this Bluetooth Low Energy (BLE) library will work, take a look at the mower compatibility list in the repo for more information.

# Requirements
You will need several python libraries:
`bleak`
`asyncio`
`json`
`asyncio_mqtt`
`automower_ble`

Install these using your package manager or pip3.
If you are on a Raspberry Pi then use your package manager to install as this is the preferred method.
```bash
apt-get install python3-bleak
apt-get install python3-asyncio_mqtt
```
Then install from pip the automower library:
```bash
pip3 install automower-ble --break-system-packages
```
(Notice the --break-system-packages that's because on debian-based systems you are asked to use the apt-get method, however not all
python libraries are available/debian packaged)

# Configuration
Modify my script to change the variables near the start for your environment.

| Configuration | Env value     | Description                       |
| :-------- | :------- | :-------------------------------- |
| LOCAL_LIB      | `/usr/src/AutoMower-BLE.git` | You can custom/specify the location to the Automower git clone repo, this is to override the pip3 install location if you wish |
| MQTT_BROKER | `MQTT_HOST` | The host of your MQTT broker, normally this is your home assistant LAN IP address |
| MQTT_PORT | `MQTT_PORT` | This is the MQTT broker port, normally 1883 |
| MQTT_USERNAME | `MQTT_USER` | Username for MQTT to authenticate with, normally mqtt |
| MQTT_PASSWORD | `MQTT_PASS` | Password for MQTT to authenticate with, this is set in Home Assistant |
| MQTT_BASE_TOPIC | `MOWER_BASE_TOPIC` | This creates the base topic used by MQTT, by default use this: homeassistant/mower/automower_ble |
| POLL_INTERVAL | `MOWER_POLL` | How often (in seconds) should we poll the mower for it's status. Don't set too rapid. 30-60seconds should do. |
| MOWER_ADDRESS | `MOWER_ADDRESS` | This is the MAC address of your mower. Find this in your app or by discovering with the library |
| MOWER_PIN | `MOWER_PIN` | This is the PIN as set on the mower, check the library out to find why or how to use this |

# Use
To use, simply run the script:
```bash
python3 mower_mqtt.py
```

# Output
The script will produce the following MQTT output in Home Assistant. You can then use the sensors as you wish. The two switches created are for PARK and MOW.

<img width="1108" height="822" alt="image" src="https://github.com/user-attachments/assets/8e3147d8-f535-4794-806f-4e6774c1f1a6" />


MowerState is probably the most important one to use in your automations, that will change to one of the following:
| MowerState | Description |
| :-------- | :-------------------------------- |
| OFF | Mower is powered off |
| WAIT_FOR_SAFETYPIN | Waiting for PIN on mower |
| STOPPED | Stopped requiring manual intervention |
| FATAL_ERROR | A critical error has occurred with the mower |
| PENDING_START | About to begin an action |
| PAUSED | Paused by user |
| IN_OPERATION | Operating, the actual action is in MowerActivity |
| RESTRICTED | Waiting for an action either in calendar or to release from PARK |
| ERROR | An error has occurred and mower needs manual intervention |

## Authors

- [@andyb2000](https://www.github.com/andyb2000)

## Acknowledgements

 - [Automower-BLE](https://github.com/alistair23/AutoMower-BLE/)

