#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#  mower_mqtt.py by Andy Brown https://github.com/andyb2000/AutoMower-BLE-MQTT/
# ------------------------------------------------------------------------------
VERSION = "0.0.2"

import asyncio
import json
import logging
import os
import sys
import datetime as dt
import signal
import time

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

from asyncio_mqtt import Client as MQTTClient

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
MQTT_BROKER = os.getenv("MQTT_HOST", "192.168.0.5")
MQTT_PORT = int(os.getenv("MQTT_PORT", 1883))
MQTT_USERNAME = os.getenv("MQTT_USER", "mqtt")
MQTT_PASSWORD = os.getenv("MQTT_PASS", "mqtt")
MQTT_BASE_TOPIC = os.getenv("MOWER_BASE_TOPIC", "homeassistant/mower/automower_ble")
POLL_INTERVAL = int(os.getenv("MOWER_POLL", 60))

MOWER_ADDRESS = os.getenv("MOWER_ADDRESS", "60:98:11:22:33:44")
MOWER_PIN = int(os.getenv("MOWER_PIN", "1234"))

WATCHDOG_TIMEOUT = 120  # seconds

# ----------------------------
# Shutdown handling
# ----------------------------
shutdown_event = asyncio.Event()


def _handle_sigterm(*_):
    LOG.warning("Received termination signal, shutting down...")
    shutdown_event.set()


signal.signal(signal.SIGINT, _handle_sigterm)
signal.signal(signal.SIGTERM, _handle_sigterm)

# ----------------------------
# Watchdog
# ----------------------------
WATCHDOG_TIMEOUT = 120  # seconds
last_progress = time.time()


def watchdog_reset():
    global last_progress
    last_progress = time.time()
    signal.alarm(WATCHDOG_TIMEOUT)


async def watchdog_task(availability_topic: str):
    """Background watchdog that exits if script hangs too long."""
    while not shutdown_event.is_set():
        await asyncio.sleep(30)
        if time.time() - last_progress > WATCHDOG_TIMEOUT:
            LOG.critical("Watchdog: No activity for %d seconds, shutting down!", WATCHDOG_TIMEOUT)
            shutdown_event.set()
            try:
                async with MQTTClient(
                    hostname=MQTT_BROKER,
                    port=MQTT_PORT,
                    username=MQTT_USERNAME,
                    password=MQTT_PASSWORD,
                ) as client:
                    await client.publish(availability_topic, "offline", retain=True)
            except Exception:
                LOG.error("Failed to publish offline status to MQTT")
            os._exit(1)


def _sigalrm_handler(signum, frame):
    LOG.critical("Signal alarm triggered: operation exceeded %d seconds", WATCHDOG_TIMEOUT)
    shutdown_event.set()
    os._exit(1)


signal.signal(signal.SIGALRM, _sigalrm_handler)
signal.alarm(WATCHDOG_TIMEOUT)

# ----------------------------
# Helper Functions
# ----------------------------
def watchdog_reset():
    global last_progress
    last_progress = time.time()
async def watchdog_task(availability_topic: str):
    """Background watchdog that exits if script hangs too long."""
    while not shutdown_event.is_set():
        await asyncio.sleep(30)
        if time.time() - last_progress > WATCHDOG_TIMEOUT:
            LOG.critical("Watchdog: No activity for %d seconds, shutting down!", WATCHDOG_TIMEOUT)
            shutdown_event.set()
            try:
                async with MQTTClient(
                    hostname=MQTT_BROKER,
                    port=MQTT_PORT,
                    username=MQTT_USERNAME,
                    password=MQTT_PASSWORD,
                ) as client:
                    await client.publish(availability_topic, "offline", retain=True)
            except Exception:
                LOG.error("Failed to publish offline status to MQTT")
            os._exit(1)  # hard exit to prevent zombie process


def _sigalrm_handler(signum, frame):
    LOG.critical("Signal alarm triggered: operation exceeded %d seconds", WATCHDOG_TIMEOUT)
    shutdown_event.set()
    os._exit(1)


# Enable POSIX alarm watchdog
signal.signal(signal.SIGALRM, _sigalrm_handler)
signal.alarm(WATCHDOG_TIMEOUT)

async def connect_mower():
    LOG.info("Creating Mower instance...")
    mower = Mower(1197489078, MOWER_ADDRESS, MOWER_PIN)
    LOG.info("Connecting to mower...")
    device = await BleakScanner.find_device_by_address(MOWER_ADDRESS)
    if device is None:
        LOG.warn("Unable to connect to device address: " + mower.address)
        LOG.warn(
            "Please make sure the device address is correct, the device is powered on and nearby"
        )
        LOG.warn("FAILED TO connect to mower")
        shutdown_event.set()
        return
    await mower.connect(device)
    LOG.info("BLE connection established ✅")
    watchdog_reset()
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
            watchdog_reset()
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
        watchdog_reset()
        return
    elif cmd == "PARK":
#        for f in ["park", "return_to_base", "dock"]:
#            if hasattr(mower, f):
#                fn = getattr(mower, f)
#                LOG.info("Executing mower.%s()", f)
#                await fn() if asyncio.iscoroutinefunction(fn) else fn()
        await mower.command("SetOverrideParkUntilNextStart")
        LOG.info("Mower SetOverrideParkUntilNextStart sent")
        watchdog_reset()
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
        watchdog_reset()

# ----------------------------
# Main Async Loop
# ----------------------------
async def main():
    mower = await connect_mower()
    known_keys = set()

    while not shutdown_event.is_set():
        try:

            async with MQTTClient(
                hostname=MQTT_BROKER,
                port=MQTT_PORT,
                username=MQTT_USERNAME,
                password=MQTT_PASSWORD
            ) as client:

                async with client.unfiltered_messages() as messages:
                    await client.subscribe(f"{MQTT_BASE_TOPIC}/command")
                    LOG.info("Subscribed to %s/command", MQTT_BASE_TOPIC)

                    # Initial status and HA discovery
                    status = await collect_status(mower)
                    if status:
                        known_keys.update(status.keys())
                        await ha_discovery(client, status)
                        LOG.info("Initial Home Assistant discovery published")

                    # Status publishing loop
                    async def status_loop():
                        nonlocal known_keys
                        while not shutdown_event.is_set():
                            status = await collect_status(mower)
                            if status:
                                # Update HA discovery if new keys appear
                                new_keys = set(status.keys()) - known_keys
                                if new_keys:
                                    LOG.info("New keys detected, updating HA discovery: %s", new_keys)
                                    await ha_discovery(client, status)
                                    known_keys.update(new_keys)
                                # Publish current status
                                LOG.info("Publishing status: %s", status)
                                try:
                                    await client.publish(
                                        f"{MQTT_BASE_TOPIC}/status",
                                        json.dumps(status)
                                    )
                                except MqttError as e:
                                    LOG.error("MQTT publish error: %s", e)
                            await asyncio.sleep(POLL_INTERVAL)

                    loop_task = asyncio.create_task(status_loop())

                    # Handle incoming MQTT messages
                    async for msg in messages:
                        if shutdown_event.is_set():
                            breal
                        try:
                            payload = msg.payload.decode().strip()
                            LOG.info("Received MQTT command: %s", payload)
                            await send_command(mower, payload)
                        except Exception as e:
                            LOG.error("Error handling command: %s", e)

                    await loop_task

        except MqttError as e:
            LOG.error("MQTT loop error: %s; reconnecting in 5s", e)
            await asyncio.sleep(5)
        except Exception:
            LOG.exception("Unexpected main loop error")
            await asyncio.sleep(5)
    LOG.info("Shutting down...")
    with contextlib.suppress(Exception):
        async with MQTTClient(
            hostname=MQTT_BROKER,
            port=MQTT_PORT,
            username=MQTT_USERNAME,
            password=MQTT_PASSWORD,
        ) as client:
            await client.publish(availability_topic, "offline", retain=True)

# ----------------------------
# Run
# ----------------------------
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        LOG.info("Interrupted, shutting down...")
