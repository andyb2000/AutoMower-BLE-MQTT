#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#  mower_mqtt.py by Andy Brown https://github.com/andyb2000/AutoMower-BLE-MQTT/
# ------------------------------------------------------------------------------
VERSION = "0.0.1"

import asyncio
import json
import logging
import os
import sys
import datetime as dt

from bleak import BleakScanner

LOCAL_LIB = "/usr/src/AutoMower-BLE.git"
if LOCAL_LIB not in sys.path:
    sys.path.insert(0, LOCAL_LIB)

from automower_ble.mower import Mower
from automower_ble.protocol import (
    BLEClient,
    MowerState,
    MowerActivity,
    ModeOfOperation,
)
from automower_ble.error_codes import ErrorCodes

from aiomqtt import Client as MQTTClient
from aiomqtt import MqttError  # Ensure MqttError is explicitly imported

# ----------------------------
# Logging
# ----------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
LOG = logging.getLogger("mower_mqtt")

# ----------------------------
# Config
# ----------------------------
MQTT_BROKER = os.getenv("MQTT_HOST", "192.168.9.56")
MQTT_PORT = int(os.getenv("MQTT_PORT", 1883))
MQTT_USERNAME = os.getenv("MQTT_USER", "mqtt")
MQTT_PASSWORD = os.getenv("MQTT_PASS", "mqtt")
MQTT_BASE_TOPIC = os.getenv("MOWER_BASE_TOPIC", "homeassistant/mower/automower_ble")
POLL_INTERVAL = int(os.getenv("MOWER_POLL", 60))

MOWER_ADDRESS = os.getenv("MOWER_ADDRESS", "70:B9:50:6A:C4:92")
MOWER_PIN = int(os.getenv("MOWER_PIN", "1234"))

# ----------------------------
# Helper Functions
# ----------------------------
async def connect_mower():
    LOG.info("Creating Mower instance...")
    mower = Mower(1197489078, MOWER_ADDRESS, MOWER_PIN)
    LOG.info("Connecting to mower...")
    device = await BleakScanner.find_device_by_address(MOWER_ADDRESS)
    if device is None:
        LOG.warning("Unable to connect to device address: " + mower.address)
        LOG.warning(
            "Please make sure the device address is correct, the device is powered on and nearby"
        )
        LOG.warning("FAILED TO connect to mower")
        return
    await mower.connect(device)
    LOG.info("BLE connection established ✅")
    return mower

async def collect_status(mower):
    status = {}
    try:
        data = await mower.command("GetAllStatistics")
        LOG.info("Data collected: %s",data)
        if data:
            battery_data = await mower.command("GetBatteryLevel")
            data["Battery"] = str(battery_data)
            charging_data = await mower.command("IsCharging")
            data["Charging"] = str(charging_data)
            state_data = await mower.command("GetState")
            data["State"] = MowerState(state_data).name
            activity_data = await mower.command("GetActivity")
            data["Activity"] = MowerActivity(activity_data).name
            next_start_data = await mower.command("GetNextStartTime")
            #LOG.info("Raw start time: %s",next_start_data)
            #data["NextStartTime"] = str(dt.datetime.fromtimestamp(next_start_data, dt.UTC).strftime("%H:%M %d/%m/%Y"))
            data["NextStartSchedule"] = dt.datetime.fromtimestamp(int(next_start_data), tz=dt.timezone.utc).isoformat()
            last_error_data = await mower.command("GetMessage", messageId=0)
            data["LastError"] = ErrorCodes(last_error_data["code"]).name
            data["LastErrorSchedule"] = dt.datetime.fromtimestamp(int(last_error_data["time"]), tz=dt.timezone.utc).isoformat()
            data["CurrUpdateSchedule"] = dt.datetime.now(tz=dt.timezone.utc).isoformat()
            status.update(data)
    except Exception as e:
        LOG.warning("Failed to get status: %s", e)
    return status

async def send_command(mower, cmd):
    cmd = cmd.upper()
    if cmd == "MOW":
#        for f in ["resume", "override", "start", "mow"]:
#            if hasattr(mower, f):
#                fn = getattr(mower, f)
#                LOG.info("Executing mower.%s()", f)
#                await fn() if asyncio.iscoroutinefunction(fn) else fn()
        LOG.info("Mower start called, going to set mode and override")
        await mower.command("SetMode", mode=ModeOfOperation.AUTO)
        await mower.command("SetOverrideMow", duration=int(3600))
        await mower.command("StartTrigger")
        LOG.info("Mower StartTrigger sent")
        return
    elif cmd == "PARK":
#        for f in ["park", "return_to_base", "dock"]:
#            if hasattr(mower, f):
#                fn = getattr(mower, f)
#                LOG.info("Executing mower.%s()", f)
#                await fn() if asyncio.iscoroutinefunction(fn) else fn()
        await mower.command("SetOverrideParkUntilNextStart")
        LOG.info("Mower SetOverrideParkUntilNextStart sent")
        return
    LOG.warning("Unknown command: %s", cmd)

# ----------------------------
# Home Assistant discovery
# ----------------------------
async def ha_discovery(client, status):
    """Publish Home Assistant MQTT discovery messages for all mower statistics."""
    device_info = {
        "identifiers": ["automower_ble"],
        "name": "Automower BLE",
        "manufacturer": "Husqvarna",
        "model": "Automower BLE"
    }

    # Binary switch for MOW / PARK
    switch_config = {
        "name": "Automower Switch",
        "command_topic": f"{MQTT_BASE_TOPIC}/command",
        "state_topic": f"{MQTT_BASE_TOPIC}/status",
        "icon": "mdi:robot-mower",
        "payload_on": "MOW",
        "payload_off": "PARK",
        "unique_id": "automower_switch_01",
        "device": device_info
    }
    await client.publish(
        "homeassistant/switch/automower_ble/config",
        json.dumps(switch_config),
        retain=True
    )

    # Create a sensor for each key in the status dictionary
    for key, value in status.items():
        sensor_config = {
            "name": f"Automower {key}",
            "state_topic": f"{MQTT_BASE_TOPIC}/status",
            "value_template": f"{{{{ value_json.{key} }}}}",
            "unique_id": f"automower_{key}_01",
            "device": device_info
        }

        # Add units if applicable
        if "Time" in key or "Usage" in key:
            sensor_config["unit_of_measurement"] = "s"
        elif "NextStartSchedule" in key:
            sensor_config["device_class"] = "timestamp"
        elif "LastError" in key:
            sensor_config["icon"] = "mdi:alert"
            sensor_config["entity_category"] = "diagnostic"
        elif "LastErrorSchedule" in key:
            sensor_config["device_class"] = "timestamp"
        elif "CurrUpdateSchedule" in key:
            sensor_config["device_class"] = "timestamp"
        elif "Charging" in key:
            sensor_config["component"] = "binary_sensor"
            sensor_config["device_class"] = "battery_charging"
        elif "State" in key:
            sensor_config["icon"] = "mdi:state-machine"
        elif "Activity" in key:
            sensor_config["icon"] = "mdi:progress-clock"
        elif "Battery" in key:
            sensor_config["device_class"] = "battery"
            sensor_config["unit_of_measurement"] = "%"
        elif "number" in key.lower():
            sensor_config["unit_of_measurement"] = None  # count

        await client.publish(
            f"homeassistant/sensor/automower_ble_{key.lower()}/config",
            json.dumps(sensor_config),
            retain=True
        )
        LOG.info("Published HA discovery for sensor: %s", key)

# ----------------------------
# Main Async Loop
# ----------------------------
async def main():
    mower = None
    while mower is None:
        LOG.info("Attempting to connect to mower...")
        mower_instance = await connect_mower()
        if mower_instance:
            mower = mower_instance
            LOG.info("Verified connection!")
        else:
            await asyncio.sleep(30)

    # Use a dictionary or a class to keep track of keys 
    # This avoids the 'nonlocal' scoping headache entirely
    context = {"known_keys": set()}

    while True:
        try:
            async with MQTTClient(
                hostname=MQTT_BROKER,
                port=MQTT_PORT,
                username=MQTT_USERNAME,
                password=MQTT_PASSWORD
            ) as client:
                
                await client.subscribe(f"{MQTT_BASE_TOPIC}/command")
                
                # Initial status
                status = await collect_status(mower)
                if status:
                    context["known_keys"].update(status.keys())
                    await ha_discovery(client, status)
                    await client.publish(f"{MQTT_BASE_TOPIC}/status", json.dumps(status))

                async def status_loop():
                    while True:
                        await asyncio.sleep(POLL_INTERVAL)
                        current_status = await collect_status(mower)
                        if current_status:
                            # Access the set via the dictionary key
                            new_keys = set(current_status.keys()) - context["known_keys"]
                            if new_keys:
                                LOG.info("New sensors detected: %s", new_keys)
                                await ha_discovery(client, current_status)
                                context["known_keys"].update(new_keys)
                            
                            await client.publish(f"{MQTT_BASE_TOPIC}/status", json.dumps(current_status))

                status_task = asyncio.create_task(status_loop())

                # The command listener keeps the connection open
                async for message in client.messages:
                    payload = message.payload.decode().strip()
                    LOG.info("Received MQTT command: %s", payload)
                    await send_command(mower, payload)
                
                status_task.cancel()

        except Exception as e:
            LOG.error("Loop error: %s. Reconnecting in 10s...", e)
            await asyncio.sleep(10)

# ----------------------------
# Run
# ----------------------------
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        LOG.info("Interrupted, shutting down...")
    except Exception as e:
        print(f"Kernel Panic: {e}")
