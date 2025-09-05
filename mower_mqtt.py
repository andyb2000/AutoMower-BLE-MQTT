#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#  mower_mqtt.py by Andy Brown https://github.com/andyb2000/AutoMower-BLE-MQTT/
# ------------------------------------------------------------------------------
VERSION = "0.0.4"

import asyncio
import json
import logging
import os
import sys
import datetime as dt
import signal
import contextlib
from dataclasses import dataclass
from typing import Dict, Any, Optional

from bleak import BleakScanner
import aiomqtt

# Add local library path
LOCAL_LIB = "/usr/src/AutoMower-BLE.git"
if LOCAL_LIB not in sys.path:
    sys.path.insert(0, LOCAL_LIB)

from automower_ble.mower import Mower
from automower_ble.protocol import MowerState, MowerActivity, ModeOfOperation
from automower_ble.error_codes import ErrorCodes

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
# Configuration
# ----------------------------
@dataclass
class Config:
    mqtt_broker: str = os.getenv("MQTT_HOST", "192.168.0.5")
    mqtt_port: int = int(os.getenv("MQTT_PORT", 1883))
    mqtt_username: str = os.getenv("MQTT_USER", "mqtt")
    mqtt_password: str = os.getenv("MQTT_PASS", "mqtt")
    mqtt_base_topic: str = os.getenv("MOWER_BASE_TOPIC", "homeassistant/mower/automower_ble")
    poll_interval: int = int(os.getenv("MOWER_POLL", 60))
    mower_address: str = os.getenv("MOWER_ADDRESS", "60:98:11:22:33:44")
    mower_pin: int = int(os.getenv("MOWER_PIN", "1234"))

CFG = Config()

# ----------------------------
# Shutdown handling
# ----------------------------
shutdown_event = asyncio.Event()

async def shutdown():
    LOG.info("Shutting down tasks...")
    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    LOG.info("All tasks cancelled.")

def _handle_sigterm(*_):
    LOG.warning("Received termination signal, shutting down...")
    shutdown_event.set()
    # Stop the event loop
    loop = asyncio.get_event_loop()
    loop.create_task(shutdown())

signal.signal(signal.SIGINT, _handle_sigterm)
signal.signal(signal.SIGTERM, _handle_sigterm)

# ----------------------------
# Heartbeat Watchdog
# ----------------------------
WATCHDOG_TIMEOUT = 180  # seconds before we assume total deadlock
last_heartbeat = 0


def watchdog_reset():
    global last_heartbeat
    last_heartbeat = asyncio.get_event_loop().time()


async def heartbeat_task(availability_topic: str):
    """Heartbeat watchdog: ensures event loop is alive."""
    watchdog_reset()
    while not shutdown_event.is_set():
        await asyncio.sleep(10)
        now = asyncio.get_event_loop().time()
        if now - last_heartbeat > WATCHDOG_TIMEOUT:
            LOG.critical("Watchdog: Event loop stalled for >%d seconds, shutting down!", WATCHDOG_TIMEOUT)
            shutdown_event.set()
            try:
                async with aiomqtt.Client(
                    hostname=CFG.mqtt_broker,
                    port=CFG.mqtt_port,
                    username=CFG.mqtt_username,
                    password=CFG.mqtt_password,
                ) as client:
                    await client.publish(availability_topic, "offline", retain=True)
            except Exception:
                LOG.error("Failed to publish offline status to MQTT")
            os._exit(1)

# ----------------------------
# Helper Functions
# ----------------------------
async def connect_mower() -> Optional[Mower]:
    """Connect to the mower over BLE."""
    try:
        LOG.info("Scanning for mower at %s...", CFG.mower_address)
        mower = Mower(1197489078, CFG.mower_address, CFG.mower_pin)
        device = await BleakScanner.find_device_by_address(CFG.mower_address)
        if not device:
            LOG.error(
                "Unable to connect to mower %s. Ensure it is powered on and nearby.",
                CFG.mower_address,
            )
            return None
        await mower.connect(device)
        LOG.info("BLE connection established ✅")
        return mower
    except Exception:
        LOG.exception("Failed to connect to mower")
        return None


async def safe_mower_command(mower: Mower, cmd: str, **kwargs) -> Any:
    """Run mower command safely with timeout + retries."""
    retries = 3
    for attempt in range(1, retries + 1):
        try:
            return await asyncio.wait_for(mower.command(cmd, **kwargs), timeout=10)
        except asyncio.TimeoutError:
            LOG.warning("Mower command %s timed out (attempt %d/%d)", cmd, attempt, retries)
        except Exception as e:
            LOG.error("Mower command %s failed: %s (attempt %d/%d)", cmd, e, attempt, retries)
        await asyncio.sleep(2 * attempt)  # backoff
    LOG.critical("Mower unresponsive after %d attempts, exiting.", retries)
    shutdown_event.set()
    return None


async def collect_status(mower: Mower) -> Dict[str, Any]:
    """Collect mower status asynchronously with error handling."""
    status: Dict[str, Any] = {}
    try:
        data = await safe_mower_command(mower, "GetAllStatistics")
        if not data:
            return status

        battery, charging, state, activity, next_start, last_error = await asyncio.gather(
            safe_mower_command(mower, "GetBatteryLevel"),
            safe_mower_command(mower, "IsCharging"),
            safe_mower_command(mower, "GetState"),
            safe_mower_command(mower, "GetActivity"),
            safe_mower_command(mower, "GetNextStartTime"),
            safe_mower_command(mower, "GetMessage", messageId=0),
        )

        if None in (battery, charging, state, activity, next_start, last_error):
            LOG.error("One or more mower commands failed, skipping this poll cycle")
            return status

        status.update(
            Battery=str(battery),
            Charging="ON" if charging else "OFF",
            State=MowerState(state).name,
            Activity=MowerActivity(activity).name,
            NextStartSchedule=dt.datetime.fromtimestamp(int(next_start), tz=dt.timezone.utc).isoformat(),
            LastError=ErrorCodes(last_error["code"]).name,
            LastErrorSchedule=dt.datetime.fromtimestamp(int(last_error["time"]), tz=dt.timezone.utc).isoformat(),
            CurrUpdateSchedule=dt.datetime.now(tz=dt.timezone.utc).isoformat(),
        )

        LOG.info(
            "Status: Battery=%s%%, Charging=%s, State=%s, Activity=%s",
            status["Battery"],
            status["Charging"],
            status["State"],
            status["Activity"],
        )
    except Exception:
        LOG.exception("Unexpected error collecting mower status")
    return status


async def send_command(mower: Mower, cmd: str) -> None:
    """Send control commands to the mower."""
    cmd = cmd.upper()
    try:
        if cmd == "MOW":
            LOG.info("Mower start sequence initiated")
            await mower.command("SetMode", mode=ModeOfOperation.AUTO)
            await mower.command("SetOverrideMow", duration=3600)
            await mower.command("StartTrigger")
            LOG.info("Mower started ✅")
        elif cmd == "PARK":
            await mower.command("SetOverrideParkUntilNextStart")
            LOG.info("Mower parked ⛔")
        else:
            LOG.warning("Unknown command received: %s", cmd)
    except Exception:
        LOG.exception("Failed to send command: %s", cmd)


# ----------------------------
# Home Assistant discovery
# ----------------------------
async def ha_discovery(client: aiomqtt.Client, status: Dict[str, Any]) -> None:
    """Publish Home Assistant MQTT discovery messages."""
    device_info = {
        "identifiers": ["automower_ble"],
        "name": "Automower BLE",
        "manufacturer": "Husqvarna",
        "model": "Automower BLE",
    }

    availability_topic = f"{CFG.mqtt_base_topic}/availability"
    await client.publish(availability_topic, "online", retain=True)

    # Binary switch
    switch_config = {
        "name": "Automower Control",
        "command_topic": f"{CFG.mqtt_base_topic}/command",
        "state_topic": f"{CFG.mqtt_base_topic}/status",
        "availability_topic": availability_topic,
        "icon": "mdi:robot-mower",
        "payload_on": "MOW",
        "payload_off": "PARK",
        "unique_id": "automower_switch_01",
        "device": device_info,
    }
    await client.publish(
        "homeassistant/switch/automower_ble/config",
        json.dumps(switch_config),
        retain=True,
    )

    sensor_mappings = {
        "Battery": {"device_class": "battery", "unit_of_measurement": "%"},
        "Charging": {"component": "binary_sensor", "device_class": "battery_charging", "payload_on": "ON", "payload_off": "OFF"},
        "State": {"icon": "mdi:state-machine"},
        "Activity": {"icon": "mdi:progress-clock"},
        "LastError": {"icon": "mdi:alert", "entity_category": "diagnostic"},
        "NextStartSchedule": {"device_class": "timestamp"},
        "LastErrorSchedule": {"device_class": "timestamp", "entity_category": "diagnostic"},
        "CurrUpdateSchedule": {"device_class": "timestamp", "entity_category": "diagnostic"},
    }

    for key in status.keys():
        mapping = sensor_mappings.get(key, {})
        component = mapping.pop("component", "sensor")

        sensor_config = {
            "name": f"Automower {key}",
            "state_topic": f"{CFG.mqtt_base_topic}/status",
            "availability_topic": availability_topic,
            "value_template": f"{{{{ value_json.{key} }}}}",
            "unique_id": f"automower_{key}_01",
            "device": device_info,
            **mapping,
        }

        await client.publish(
            f"homeassistant/{component}/automower_ble_{key.lower()}/config",
            json.dumps(sensor_config),
            retain=True,
        )
        LOG.debug("Published HA discovery for %s (%s)", key, component)


# ----------------------------
# Main Loop
# ----------------------------
async def main() -> None:
    mower = await connect_mower()
    if not mower:
        LOG.critical("Unable to connect to mower, exiting.")
        return

    known_keys: set[str] = set()
    availability_topic = f"{CFG.mqtt_base_topic}/availability"
    asyncio.create_task(heartbeat_task(availability_topic))

    while not shutdown_event.is_set():
        try:
            async with aiomqtt.Client(
                hostname=CFG.mqtt_broker,
                port=CFG.mqtt_port,
                username=CFG.mqtt_username,
                password=CFG.mqtt_password,
            ) as client:

                await client.publish(availability_topic, "online", retain=True)
                LOG.info("MQTT connected ✅")

                await client.subscribe(f"{CFG.mqtt_base_topic}/command")
                LOG.info("Subscribed to %s/command", CFG.mqtt_base_topic)

                status = await collect_status(mower)
                if status:
                    known_keys.update(status.keys())
                    await ha_discovery(client, status)
                    LOG.info("Initial HA discovery done")

                async def status_loop():
                    nonlocal known_keys
                    while not shutdown_event.is_set():
                        status = await collect_status(mower)
                        if status:
                            new_keys = set(status.keys()) - known_keys
                            if new_keys:
                                await ha_discovery(client, status)
                                known_keys.update(new_keys)
                            try:
                                await client.publish(
                                    f"{CFG.mqtt_base_topic}/status", json.dumps(status)
                                )
                            except Exception:
                                LOG.exception("MQTT publish error")
                                break
                        await asyncio.sleep(CFG.poll_interval)
                        watchdog_reset()  # confirm loop is alive

                loop_task = asyncio.create_task(status_loop())

                async for msg in client.messages:
                    if shutdown_event.is_set():
                        break
                    payload = msg.payload.decode().strip()
                    LOG.info("MQTT command received: %s", payload)
                    await send_command(mower, payload)
                    watchdog_reset()

                await loop_task

        except Exception as e:
            LOG.error("MQTT loop error: %s", e)
            await asyncio.sleep(5)

    LOG.info("Shutting down...")
    with contextlib.suppress(Exception):
        async with aiomqtt.Client(
            hostname=CFG.mqtt_broker,
            port=CFG.mqtt_port,
            username=CFG.mqtt_username,
            password=CFG.mqtt_password,
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
