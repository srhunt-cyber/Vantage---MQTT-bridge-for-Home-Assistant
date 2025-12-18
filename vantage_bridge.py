#!/usr/bin/env python3
"""
Vantage <-> MQTT bridge
Version 1.1.1-SniperFix

- BASE: v1.0.9 (Safe Logging + Throttle).
- FIX: Re-connected the "Sniper" trigger so button presses force an immediate poll.
"""

import asyncio
import json
import logging
import os
import re
import signal
import socket
import ssl
import time
from typing import Any, Dict, List, Optional, Set, Tuple
from collections import defaultdict

import psutil
from aiomqtt import Client, Message, Will, TLSParameters
from aiovantage import Vantage
from dotenv import load_dotenv

# ─────────────────────────────────────────────────────────────────────────────
# Load .env
# ─────────────────────────────────────────────────────────────────────────────

load_dotenv()

# ─────────────────────────────────────────────────────────────────────────────
# Logging Setup
# ─────────────────────────────────────────────────────────────────────────────

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
_requested_level = getattr(logging, LOG_LEVEL, logging.INFO)
ROOT_LEVEL = logging.INFO if _requested_level < logging.INFO else _requested_level

root_logger = logging.getLogger()
root_logger.setLevel(ROOT_LEVEL)

for h in list(root_logger.handlers):
    root_logger.removeHandler(h)

formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
console = logging.StreamHandler()
console.setLevel(ROOT_LEVEL)
console.setFormatter(formatter)
root_logger.addHandler(console)

log = logging.getLogger("vantage_mqtt_bridge")
logging.getLogger("aiomqtt").setLevel(logging.WARNING)

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

VANTAGE_HOST = os.getenv("VANTAGE_HOST")
if not VANTAGE_HOST:
    raise ValueError("VANTAGE_HOST must be set in .env")

VANTAGE_HOST_SAFE = VANTAGE_HOST.replace(".", "_")
VANTAGE_USER = os.getenv("VANTAGE_USER") or None
VANTAGE_PASS = os.getenv("VANTAGE_PASS") or None

MQTT_HOST = os.getenv("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USERNAME = os.getenv("MQTT_USERNAME") or None
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD") or None
MQTT_TLS_ENABLED = os.getenv("MQTT_TLS_ENABLED", "false").lower() == "true"

BASE_TOPIC = os.getenv("BASE_TOPIC", "vantage")
DISCOVERY_PREFIX = os.getenv("DISCOVERY_PREFIX", "homeassistant")

AVAILABILITY_TOPIC = f"{BASE_TOPIC}/bridge/status"
BRIDGE_DEVICE_ID = f"vantage_controller_{VANTAGE_HOST_SAFE}"

# --- TUNING ---
RECONNECT_DELAY_MQTT = 10
HEALTH_CHECK_INTERVAL = 30
ENABLE_FALLBACK_POLLING = True
POLL_INTERVAL = 90
POLL_QUIET_TIME = 5 # Reduced to 5s so Sniper can fire quickly after a scene

# THROTTLE: 0.02s delay to prevent crashes
COMMAND_THROTTLE_DELAY = 0.02

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def ha_to_vantage_level(brightness: int) -> float:
    try:
        bri = int(brightness)
    except (TypeError, ValueError):
        bri = 0
    bri = max(0, min(255, bri))
    return (bri / 255.0) * 100.0


def vantage_to_ha_brightness(level: float) -> int:
    try:
        lvl = float(level)
    except (TypeError, ValueError):
        lvl = 0.0
    lvl = max(0.0, min(100.0, lvl))
    return int(round((lvl / 100.0) * 255.0))


def slugify(name: str) -> str:
    s = (name or "").lower()
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s or "load"


# ─────────────────────────────────────────────────────────────────────────────
# Aiovantage "tap" logging handler
# ─────────────────────────────────────────────────────────────────────────────

class _AiovantageTapHandler(logging.Handler):
    def __init__(self, bridge: "KeypadEventsBridge"):
        super().__init__(level=logging.DEBUG)
        self.bridge = bridge

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)

        if "EL:" not in msg:
            return

        try:
            self.bridge._loop.call_soon_threadsafe(
                self.bridge._handle_el_line, msg
            )
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Keypad Events Bridge (With Sniper Trigger Re-Added)
# ─────────────────────────────────────────────────────────────────────────────

class KeypadEventsBridge:
    MANUFACTURER = "Legrand Vantage"
    MODEL = "Keypad"

    def __init__(
        self,
        vantage: Vantage,
        get_mqtt_client: callable,
        poll_trigger: asyncio.Event, # <--- RESTORED
        base_prefix: str = "vantage",
        discovery_prefix: str = "homeassistant",
        learn_mode: bool = True,
        include_stations: Optional[Set[int]] = None,
        publish_raw: bool = True,
    ):
        self.vantage = vantage
        self.get_mqtt_client = get_mqtt_client
        self.poll_trigger = poll_trigger # <--- RESTORED
        self.base_prefix = base_prefix.rstrip("/")
        self.discovery_prefix = discovery_prefix.rstrip("/")
        self.learn_mode = learn_mode
        self.include_stations = include_stations
        self.publish_raw = publish_raw

        self._discovered: Set[Tuple[int, int, str]] = set()
        self._loop = asyncio.get_running_loop()
        self._area_map: Dict[int, str] = {}
        
        # Exact working regex from v1.0.8
        self._regex = re.compile(r"EL:\s+(\d+)\s+([\w\.]+)\s+(-?\d+)")
        self._tap_handler: Optional[_AiovantageTapHandler] = None

    async def start(self) -> None:
        log.info("Starting Keypad Bridge (Silent Tap Mode)...")
        await self.vantage.buttons.initialize(fetch_state=True)
        await self.vantage.tasks.initialize(fetch_state=True)

        try:
            for area in self.vantage.areas:
                self._area_map[area.id] = area.name
        except Exception:
            pass

        # SAFE LOGGING HIJACK
        aio_log = logging.getLogger("aiovantage")
        for h in list(aio_log.handlers):
            aio_log.removeHandler(h)
        aio_log.propagate = False
        aio_log.setLevel(logging.DEBUG)

        self._tap_handler = _AiovantageTapHandler(self)
        aio_log.addHandler(self._tap_handler)

        log.info(f"Keypad Bridge active.")

    def _handle_el_line(self, msg: str) -> None:
        try:
            match = self._regex.search(msg)
            if not match:
                return

            vid = int(match.group(1))
            method = match.group(2)
            val = int(match.group(3))

            if method in ["Button.GetState", "Task.IsRunning"]:
                self._loop.create_task(self._handle_tap_event(vid, method, val))
        except Exception:
            pass

    async def _handle_tap_event(self, vid, method, val) -> None:
        mqtt = self.get_mqtt_client()
        if not mqtt:
            return

        obj = self.vantage.buttons.get(vid)
        source_type = "button"
        if not obj:
            obj = self.vantage.tasks.get(vid)
            source_type = "task"

        if not obj:
            return

        station = getattr(obj, "parent", None)
        station_id = getattr(station, "vid", None) or getattr(station, "id", 0) if station else 0
        if not isinstance(station_id, int):
            station_id = 0

        station_name = "Unknown"
        suggested_area = ""
        pos = vid

        if source_type == "task":
            pos = vid
            station_name = getattr(obj, "name", "Virtual Task")
            area_id = getattr(obj, "area_id", 0)
            suggested_area = self._area_map.get(area_id, "")
        else:
            pos = getattr(obj, "location", None) or getattr(obj, "vid", vid)
            station_name = f"Keypad {station_id}"
            if station and hasattr(station, "name"):
                station_name = station.name
            area_id = getattr(station, "area_id", 0) if station else 0
            suggested_area = self._area_map.get(area_id, "")

        if self.include_stations and source_type == "button" and station_id not in self.include_stations:
            return

        action = "press" if val == 1 else "release" if val == 0 else "unknown"
        if action == "unknown":
            return

        # --- SNIPER LOGIC RESTORED ---
        # If we see a PRESS, tell the main loop to wake up!
        if action == "press" and self.poll_trigger:
            log.info(f"Sniper Trigger: Button {pos} pressed. Scheduling fast poll.")
            self.poll_trigger.set()
        # -----------------------------

        if self.publish_raw:
            raw_topic = f"{self.base_prefix}/keypad/_raw"
            raw_payload = {
                "type": source_type,
                "name": station_name,
                "area": suggested_area,
                "id": vid,
                "pos": pos,
                "action": action,
                "val": val,
            }
            try:
                await mqtt.publish(raw_topic, json.dumps(raw_payload), qos=0)
            except Exception:
                pass

        target_id = station_id if station_id != 0 else f"task_{vid}"
        topic = f"{self.base_prefix}/keypad/{target_id}/button/{pos}/action"

        try:
            await mqtt.publish(topic, action, qos=0)
            log.debug(f"EVENT: {station_name} Btn:{pos} -> {action}")
        except Exception:
            pass

        if self.learn_mode and (target_id, pos, action) not in self._discovered:
            await self._publish_disc(mqtt, target_id, station_name, suggested_area, pos, topic, action, source_type)
            self._discovered.add((target_id, pos, action))

    async def _publish_disc(self, mqtt, uid, name, area, pos, topic, action, stype) -> None:
        dev_id = f"vantage_kp_{uid}"
        subtype = f"{stype}_{pos}"
        disc_topic = f"{self.discovery_prefix}/device_automation/{dev_id}_{subtype}_{action}/config"
        ha_type = "button_short_press" if action == "press" else "button_short_release"

        payload = {
            "platform": "device_automation",
            "automation_type": "trigger",
            "type": ha_type,
            "subtype": subtype,
            "topic": topic,
            "payload": action,
            "device": {
                "identifiers": [dev_id],
                "name": name,
                "manufacturer": self.MANUFACTURER,
                "model": f"Vantage {stype.capitalize()}",
                "suggested_area": area,
                "via_device": BRIDGE_DEVICE_ID,
            },
        }
        try:
            await mqtt.publish(disc_topic, json.dumps(payload), qos=1, retain=True)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Main Bridge
# ─────────────────────────────────────────────────────────────────────────────

class VantageBridge:
    def __init__(self):
        self._loop = asyncio.get_event_loop()
        self._shutdown_requested = False
        self._vantage: Optional[Vantage] = None
        self._mqtt_client: Optional[Client] = None
        self._mqtt_connected = False
        self._mqtt_task: Optional[asyncio.Task] = None
        self._keypad_bridge: Optional[KeypadEventsBridge] = None
        self._loads: Dict[int, Any] = {}
        self._is_dimmable: Dict[int, bool] = {}
        self._obj_id_map: Dict[int, str] = {}
        self._area_names: Dict[int, str] = {}
        self._modules: Dict[int, Any] = {}
        self._last_non_zero_level: Dict[int, float] = {}
        self._start_time = time.monotonic()
        self._process = psutil.Process(os.getpid())
        self._total_publishes = 0
        self._last_event_time = time.monotonic()
        self._health_task: Optional[asyncio.Task] = None
        self._poll_task: Optional[asyncio.Task] = None
        
        # EVENT FOR SNIPER POLLING
        self._poll_trigger = asyncio.Event()

    def _ha_config_topic(self, component, object_id):
        return f"{DISCOVERY_PREFIX}/{component}/{BASE_TOPIC}/{object_id}/config"

    def _topic(self, *parts):
        return f"{BASE_TOPIC}/{'/'.join(map(str, parts))}"

    def get_mqtt_client(self) -> Optional[Client]:
        if self._mqtt_connected and self._mqtt_client:
            return self._mqtt_client
        return None

    # ─────────────────────────────────────────────────────────────────────
    # MQTT Loop
    # ─────────────────────────────────────────────────────────────────────

    async def _mqtt_loop(self):
        log.info("Starting MQTT loop...")
        while not self._shutdown_requested:
            try:
                tls_params = TLSParameters(tls_version=ssl.PROTOCOL_TLSv1_2) if MQTT_TLS_ENABLED else None
                will = Will(topic=AVAILABILITY_TOPIC, payload=b"offline", qos=1, retain=True)

                async with Client(
                    hostname=MQTT_HOST,
                    port=MQTT_PORT,
                    username=MQTT_USERNAME,
                    password=MQTT_PASSWORD,
                    will=will,
                    tls_params=tls_params,
                ) as client:
                    log.info(f"MQTT connected to {MQTT_HOST}:{MQTT_PORT}")
                    self._mqtt_client = client
                    self._mqtt_connected = True

                    await self._publish_async(AVAILABILITY_TOPIC, "online", qos=1, retain=True)

                    if self._loads:
                        await self._publish_all_discovery_async()

                    await client.subscribe(self._topic("light", "+", "set"), qos=1)
                    await client.subscribe(self._topic("light", "+", "brightness", "set"), qos=1)

                    async for message in client.messages:
                        try:
                            await self._handle_mqtt_message_async(message)
                        except Exception as e:
                            log.error(f"Error handling MQTT message: {e}", exc_info=True)
            except Exception as e:
                log.warning(f"MQTT error: {e}. Reconnecting...")
            finally:
                self._mqtt_client = None
                self._mqtt_connected = False
                await asyncio.sleep(RECONNECT_DELAY_MQTT)

    async def _publish_async(self, topic, payload, retain=False, qos=0):
        if self._mqtt_client and self._mqtt_connected:
            try:
                await self._mqtt_client.publish(topic, payload, qos=qos, retain=retain)
                self._total_publishes += 1
            except Exception as e:
                log.error(f"MQTT publish error on {topic}: {e}")

    async def _handle_mqtt_message_async(self, message: Message):
        topic = str(message.topic)
        payload = message.payload.decode("utf-8", "ignore").strip()
        parts = topic.split("/")

        try:
            if len(parts) >= 4 and parts[0] == BASE_TOPIC and parts[1] == "light":
                load_id = int(parts[2])
                load_obj = self._loads.get(load_id)
                if not load_obj:
                    return

                # Brightness set
                if len(parts) >= 5 and parts[3] == "brightness" and parts[4] == "set":
                    level = 0.0
                    try:
                        bri = int(payload)
                        level = ha_to_vantage_level(bri)
                        if level > 0:
                            self._last_non_zero_level[load_id] = level
                    except ValueError:
                        if payload.upper() == "ON":
                            level = self._last_non_zero_level.get(load_id, 100.0)
                        else:
                            return
                    await self._set_level(load_id, level, load_obj)
                    return

                # Simple ON/OFF set
                if len(parts) == 4 and parts[3] == "set":
                    command = payload.upper()
                    if command == "ON":
                        level = self._last_non_zero_level.get(load_id, 100.0)
                        await self._set_level(load_id, level, load_obj)
                    elif command == "OFF":
                        await load_obj.turn_off()
                        await self._publish_load_state_async(load_id, 0.0)
                        # Throttle OFF commands too
                        await asyncio.sleep(COMMAND_THROTTLE_DELAY)

        except Exception as e:
            log.error(f"Error parsing MQTT cmd: {e}", exc_info=True)

    # ─────────────────────────────────────────────────────────────────────
    # Load Control & Discovery
    # ─────────────────────────────────────────────────────────────────────

    def _get_area_name(self, load):
        area = getattr(load, "area", None)
        if area and hasattr(area, "name"): return area.name
        aid = getattr(load, "area_id", 0)
        return self._area_names.get(aid, "Unassigned")

    async def _discover_loads(self):
        if not self._vantage: return
        log.info("Discovering loads...")
        self._area_names.clear()
        for area in self._vantage.areas: self._area_names[area.id] = area.name
        await self._vantage.loads.initialize()
        self._loads.clear()
        self._last_non_zero_level.clear()
        grouped_loads: Dict[str, List[Any]] = defaultdict(list)
        for load in self._vantage.loads:
            self._loads[load.id] = load
            self._is_dimmable[load.id] = bool(getattr(load, "is_dimmable", True))
            if load.level and load.level > 0: self._last_non_zero_level[load.id] = load.level
            grouped_loads[slugify(getattr(load, "name", "load"))].append(load)
        self._obj_id_map.clear()
        for base_name, loads in grouped_loads.items():
            loads.sort(key=lambda x: x.id)
            for i, load in enumerate(loads):
                oid = base_name if i == 0 else f"{base_name}_{i + 1}"
                if "fan" in (load.name or "").lower(): oid += "_load"
                self._obj_id_map[load.id] = oid
        await self._publish_bridge_device_async()
        for l in self._loads.values():
            await self._publish_discovery_for_load_async(l)
            await self._publish_attributes_for_load_async(l)
            await self._publish_load_state_async(l.id, l.level or 0.0)
        log.info(f"Discovered {len(self._loads)} loads.")

    async def _handle_load_event(self, event=None, load=None, data=None, *args, **kwargs):
        self._last_event_time = time.monotonic()
        if load is None: load = kwargs.get("load")
        if not load: return
        level = getattr(load, "level", None)
        if level is None and isinstance(data, dict): level = data.get("level", data.get("value"))
        if level is not None:
            lvl = float(level)
            if lvl > 0: self._last_non_zero_level[load.id] = lvl
            await self._publish_load_state_async(load.id, lvl)

    def _subscribe_to_load_events(self):
        if self._vantage:
            try: self._vantage.loads.subscribe("state_change", self._handle_load_event)
            except Exception: pass

    async def _set_level(self, load_id: int, level: float, load: Optional[Any] = None):
        if not self._vantage: return
        try:
            if load is None:
                load = self._loads.get(load_id) or await self._vantage.loads.aget(load_id)
            if not load: return
            level = max(0.0, min(100.0, float(level)))
            log.info(f"Setting load {load_id} to {level:.1f}%")
            await load.set_level(level)
            if level > 0: self._last_non_zero_level[load_id] = level
            await self._publish_load_state_async(load_id, level)
            await asyncio.sleep(COMMAND_THROTTLE_DELAY)
        except Exception as e:
            log.error(f"Error setting level: {e}", exc_info=True)

    # ─────────────────────────────────────────────────────────────────────
    # Publishing Logic
    # ─────────────────────────────────────────────────────────────────────
    
    async def _publish_bridge_offline_async(self):
        try:
            will = Will(AVAILABILITY_TOPIC, b"offline", qos=1, retain=True)
            async with Client(hostname=MQTT_HOST, port=MQTT_PORT, username=MQTT_USERNAME, password=MQTT_PASSWORD, will=will) as client:
                await client.publish(AVAILABILITY_TOPIC, "offline", qos=1, retain=True)
        except Exception: pass

    async def _publish_bridge_device_async(self):
        try:
            oid = f"{BRIDGE_DEVICE_ID}_status"
            topic = self._ha_config_topic("sensor", oid)
            payload = {
                "name": "Bridge Status",
                "default_entity_id": f"sensor.{oid}",
                "unique_id": f"{BRIDGE_DEVICE_ID}_status_sensor",
                "state_topic": AVAILABILITY_TOPIC,
                "icon": "mdi:bridge",
                "device": {
                    "identifiers": [BRIDGE_DEVICE_ID],
                    "name": f"Vantage Controller ({VANTAGE_HOST})",
                    "manufacturer": "Vantage",
                    "model": "InFusion (SDK) Bridge",
                    "sw_version": "1.1.1-SniperFix",
                },
                "entity_category": "diagnostic",
            }
            await self._publish_async(topic, json.dumps(payload), retain=True, qos=1)
        except Exception: pass

    async def _publish_all_discovery_async(self):
        await self._publish_bridge_device_async()
        for l in self._loads.values():
            await self._publish_discovery_for_load_async(l)
            await self._publish_attributes_for_load_async(l)

    async def _publish_discovery_for_load_async(self, load):
        if not self._mqtt_connected: return
        oid = self._obj_id_map.get(load.id)
        if not oid: return
        topic = self._ha_config_topic("light", oid)
        safe_dev_id = f"vantage_{VANTAGE_HOST_SAFE}_load_{load.id}"
        payload = {
            "name": getattr(load, "name", f"Load {load.id}"),
            "unique_id": f"vantage_{VANTAGE_HOST}_load_{load.id}_light",
            "state_topic": self._topic("light", load.id, "state"),
            "command_topic": self._topic("light", load.id, "set"),
            "brightness_state_topic": self._topic("light", load.id, "brightness", "state"),
            "brightness_command_topic": self._topic("light", load.id, "brightness", "set"),
            "brightness_scale": 255,
            "availability_topic": AVAILABILITY_TOPIC,
            "device": {
                "identifiers": [safe_dev_id],
                "name": getattr(load, "name", "Unknown"),
                "suggested_area": self._get_area_name(load),
                "manufacturer": "Vantage",
                "model": "InFusion Load",
                "via_device": BRIDGE_DEVICE_ID,
            },
        }
        await self._publish_async(topic, json.dumps(payload), retain=True, qos=1)

    async def _publish_attributes_for_load_async(self, load):
        attr_topic = self._topic("light", load.id, "attributes")
        attrs = { "vantage_area": self._get_area_name(load), "vantage_id": load.id, "vantage_name": getattr(load, "name", "load") }
        await self._publish_async(attr_topic, json.dumps(attrs), retain=True, qos=1)

    async def _publish_load_state_async(self, load_id, level):
        if level is None: return
        state_topic = self._topic("light", load_id, "state")
        state = "ON" if level > 0 else "OFF"
        await self._publish_async(state_topic, state)
        if self._is_dimmable.get(load_id, True):
            bt = self._topic("light", load_id, "brightness", "state")
            await self._publish_async(bt, str(vantage_to_ha_brightness(level)))

    async def _publish_diagnostics_async(self):
        if not self._mqtt_connected: return
        try:
            cpu = self._process.cpu_percent(interval=None)
            mem = round(self._process.memory_info().rss / (1024 * 1024), 2)
            await self._publish_async(self._topic("diagnostics", "cpu_usage_pct"), str(cpu))
            await self._publish_async(self._topic("diagnostics", "memory_usage_mb"), str(mem))
            await self._publish_async(self._topic("diagnostics", "uptime_s"), str(int(time.monotonic() - self._start_time)))
            await self._publish_async(self._topic("diagnostics", "messages_published_total"), str(self._total_publishes))
            await self._publish_async(self._topic("diagnostics", "entity_count"), str(len(self._loads)), retain=True)
        except Exception: pass

    # ─────────────────────────────────────────────────────────────────────
    # Health & Polling
    # ─────────────────────────────────────────────────────────────────────

    async def _health_check_loop(self):
        while not self._shutdown_requested:
            await asyncio.sleep(HEALTH_CHECK_INTERVAL)
            if self._mqtt_connected:
                await self._publish_async(AVAILABILITY_TOPIC, "online", retain=True)
                await self._publish_diagnostics_async()

    async def _poll_loop(self):
        log.info(f"Starting Smart Polling Loop (Interval: {POLL_INTERVAL}s).")
        await asyncio.sleep(10) # Initial grace period

        while not self._shutdown_requested:
            try:
                # WAIT FOR EITHER:
                # 1. 90 seconds (Normal Loop)
                # 2. Sniper Trigger (Button Press)
                await asyncio.wait_for(self._poll_trigger.wait(), timeout=POLL_INTERVAL)
                
                # If we get here, the Sniper triggered!
                log.info("Sniper Trigger Detected. Waiting 5s for scene to finish...")
                self._poll_trigger.clear()
                
                # Wait for the fade to complete
                await asyncio.sleep(5)
                
            except asyncio.TimeoutError:
                # Normal 90s timeout - just continue to check
                pass

            # Smart Check: Don't poll if we just got live data
            time_since_activity = time.monotonic() - self._last_event_time
            if time_since_activity < POLL_QUIET_TIME:
                continue

            if self._vantage:
                try:
                    log.info("Running Update Poll...")
                    await self._vantage.loads.fetch_state()
                    self._last_event_time = time.monotonic()
                    for load_obj in self._vantage.loads:
                        if load_obj.level is not None:
                            lvl = float(load_obj.level)
                            if lvl > 0: self._last_non_zero_level[load_obj.id] = lvl
                            await self._publish_load_state_async(load_obj.id, lvl)
                except Exception as e:
                    log.warning(f"Poll error: {e}")

    # ─────────────────────────────────────────────────────────────────────
    # Run
    # ─────────────────────────────────────────────────────────────────────

    async def run(self):
        log.info("Starting Vantage MQTT Bridge (v1.1.1-SniperFix)...")
        for sig in (signal.SIGINT, signal.SIGTERM):
            try: self._loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(self.stop(s)))
            except NotImplementedError: pass

        self._mqtt_task = self._loop.create_task(self._mqtt_loop())

        while not self._shutdown_requested:
            try:
                log.info(f"Connecting to Vantage at {VANTAGE_HOST} ...")
                async with Vantage(VANTAGE_HOST, VANTAGE_USER, VANTAGE_PASS, ssl=False) as vantage:
                    log.info("Vantage connected.")
                    self._vantage = vantage
                    self._last_event_time = time.monotonic()
                    await vantage.areas.initialize()
                    await vantage.modules.initialize()
                    await self._discover_loads()
                    self._subscribe_to_load_events()

                    # Start Keypad Bridge (Pass Poll Trigger!)
                    self._keypad_bridge = KeypadEventsBridge(
                        vantage,
                        self.get_mqtt_client,
                        self._poll_trigger, # <--- RESTORED THE WIRE!
                        BASE_TOPIC,
                        DISCOVERY_PREFIX,
                        learn_mode=True,
                        publish_raw=True,
                    )
                    await self._keypad_bridge.start()

                    if not self._health_task: self._health_task = self._loop.create_task(self._health_check_loop())
                    if ENABLE_FALLBACK_POLLING and not self._poll_task: self._poll_task = self._loop.create_task(self._poll_loop())
                    if self._mqtt_connected: await self._publish_async(AVAILABILITY_TOPIC, "online", retain=True, qos=1)

                    while not self._shutdown_requested: await asyncio.sleep(1)

            except asyncio.CancelledError: break
            except Exception as e:
                log.error(f"Vantage connection error: {e}")
                self._vantage = None
                if not self._shutdown_requested: await asyncio.sleep(RECONNECT_DELAY_MQTT)

    async def stop(self, sig=None):
        if self._shutdown_requested: return
        self._shutdown_requested = True
        log.info("Shutdown requested.")
        for task in (self._health_task, self._poll_task, self._mqtt_task):
            if task:
                task.cancel()
                try: await task
                except asyncio.CancelledError: pass
        await self._publish_bridge_offline_async()
        log.info("Bridge stopped.")

async def main():
    bridge = VantageBridge()
    try: await bridge.run()
    finally:
        if not bridge._shutdown_requested: await bridge.stop()

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: pass
