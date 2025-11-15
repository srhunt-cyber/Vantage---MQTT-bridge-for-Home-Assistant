#!/usr/bin/env python3
"""
Vantage <-> MQTT bridge
Version 0.9.11 — Fixes on/off for dimmable loads
- FIX 11: Updated discovery to set command_topic to /set for dimmable loads, keeping on_command_type: 'brightness'.
- FIX 12: Handle 'ON'/'OFF' on /set for all loads (fallback), mapping to level 100/0.
- Removed ignoring /set for dimmable; now processes them.
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
from typing import Any, Dict, List, Optional
from collections import defaultdict
# --- INSTRUMENTATION ---
import psutil # REQUIRED FOR METRICS
# --- INSTRUMENTATION ---
from aiomqtt import Client, Message, Will, TLSParameters
from aiovantage import Vantage
from dotenv import load_dotenv
# ─────────────────────────────────────────────────────────────────────────────
# Load .env
# ─────────────────────────────────────────────────────────────────────────────
load_dotenv()
# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
log = logging.getLogger("vantage_mqtt_bridge")
logging.getLogger("aiomqtt").setLevel(logging.WARNING)
logging.getLogger("aiovantage").setLevel(logging.INFO) # set DEBUG for protocol chatter
# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────
VANTAGE_HOST = os.getenv("VANTAGE_HOST", "192.168.1.39")
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
# Timing
RECONNECT_DELAY_MQTT = 10
HEALTH_CHECK_INTERVAL = 30 # Interval for sending metrics and heartbeat
ENABLE_FALLBACK_POLLING = True
POLL_INTERVAL = 60
# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
_slug_re = re.compile(r"[^a-z0-9]+")
def slugify(name: str) -> str:
    s = (name or "").lower()
    s = _slug_re.sub("_", s).strip("_")
    return s or "load"
# ─────────────────────────────────────────────────────────────────────────────
# Bridge
# ─────────────────────────────────────────────────────────────────────────────
class VantageBridge:
    def __init__(self):
        self._loop = asyncio.get_event_loop()
        self._shutdown_requested = False
        # Vantage
        self._vantage: Optional[Vantage] = None
        # MQTT
        self._mqtt_client: Optional[Client] = None
        self._mqtt_connected = False
        self._mqtt_task: Optional[asyncio.Task] = None
        # Discovery & state
        self._loads: Dict[int, Any] = {} # id -> load
        self._is_dimmable: Dict[int, bool] = {} # id -> bool
        self._obj_id_map: Dict[int, str] = {} # id -> object_id (e.g., "fixture_7")
        self._area_names: Dict[int, str] = {} # id -> area name
        self._modules: Dict[int, Any] = {} # id -> module object
        # --- INSTRUMENTATION ---
        self._start_time = time.monotonic()
        self._pid = os.getpid()
        self._process = psutil.Process(self._pid)
        self._total_publishes = 0
        self._vantage_connect_time: Optional[float] = None
        # --- INSTRUMENTATION ---
        # Health
        self._last_event_time = time.monotonic()
        self._health_task: Optional[asyncio.Task] = None
        self._poll_task: Optional[asyncio.Task] = None
    # Topics
    def _ha_config_topic(self, component: str, object_id: str) -> str:
        return f"{DISCOVERY_PREFIX}/{component}/{BASE_TOPIC}/{object_id}/config"
    def _topic(self, *parts) -> str:
        return f"{BASE_TOPIC}/{'/'.join(map(str, parts))}"
    # ───────────── MQTT (async) ─────────────
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
                    # Online
                    await self._publish_async(AVAILABILITY_TOPIC, "online", qos=1, retain=True)
                    # If we already have loads, republish discovery (e.g., HA restart)
                    if self._loads:
                        await self._publish_all_discovery_async()
                    # Subscribe commands
                    cmd1 = self._topic("light", "+", "set")
                    cmd2 = self._topic("light", "+", "brightness", "set")
                    log.info(f"Subscribing to commands: {cmd1}, {cmd2}")
                    await client.subscribe(cmd1, qos=1)
                    await client.subscribe(cmd2, qos=1)
                    async for message in client.messages:
                        try:
                            await self._handle_mqtt_message_async(message)
                        except Exception as e:
                            log.error(f"Error handling MQTT message {message.topic}: {e}", exc_info=True)
            except Exception as e:
                log.warning(f"MQTT connection error ({e}). Reconnecting in {RECONNECT_DELAY_MQTT}s.")
            finally:
                self._mqtt_client = None
                self._mqtt_connected = False
                await self._publish_bridge_offline_async()
                if not self._shutdown_requested:
                    await asyncio.sleep(RECONNECT_DELAY_MQTT)
    async def _publish_async(self, topic: str, payload: str, retain=False, qos=0):
        if self._mqtt_client and self._mqtt_connected:
            try:
                await self._mqtt_client.publish(topic, payload, qos=qos, retain=retain)
                self._total_publishes += 1 # --- INSTRUMENTATION ---
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
                if load_obj is None:
                    log.warning(f"Received command for unknown load ID: {load_id}")
                    return
                is_dim = self._is_dimmable.get(load_id, True)
                log.debug(f"Handling command for Load {load_id} ({load_obj.name}) [Dimmable: {is_dim}]: Topic={topic}, Payload='{payload}'")
                # 1. Brightness Command (e.g. .../brightness/set)
                # This handles dimming and can also handle ON/OFF if payload is 255 or 0
                if len(parts) >= 5 and parts[3] == "brightness" and parts[4] == "set":
                    try:
                        bri = int(payload) # 0..255 from HA
                    except ValueError:
                        log.warning(f"Invalid brightness payload for load {load_id}: '{payload}'")
                        return
                    bri = max(0, min(255, bri))
                    # Convert 0-255 to 0.0-100.0% for Vantage
                    level = max(0.0, min(100.0, (bri / 255.0) * 100.0))
                    log.info(f"BRIGHTNESS CMD: Load {load_id} ({load_obj.name}): HA Brightness {bri} -> Vantage Level {level:.1f}%")
                    await self._set_level(load_id, level)
                    return
                # 2. ON/OFF Command (e.g. .../set)
                # FIX 12: Handle for all loads, mapping ON/OFF to 100/0
                if len(parts) == 4 and parts[3] == "set":
                    command = payload.upper()
                    if command == "OFF":
                        level = 0.0
                    elif command == "ON":
                        level = 100.0
                    else:
                        log.warning(f"Invalid ON/OFF payload for load {load_id}: '{payload}'")
                        return
                    log.info(f"ON/OFF CMD: Load {load_id} ({load_obj.name}): {command} -> Level {level:.1f}%")
                    await self._set_level(load_id, level)
                    return
        except Exception as e:
            log.error(f"Error parsing MQTT cmd {topic}='{payload}': {e}", exc_info=True)
    # ───────────── Vantage ─────────────
    async def _discover_loads(self):
        if not self._vantage:
            return
        # Build the Area (Room) lookup table
        log.info("Building area lookup table...")
        self._area_names.clear()
        try:
            for area in self._vantage.areas:
                self._area_names[area.id] = area.name
            log.info(f"Found {len(self._area_names)} areas.")
        except Exception as e:
            log.error(f"Error building area table: {e}")
        # Build the Module lookup table
        log.info("Building module lookup table...")
        self._modules.clear()
        try:
            for module in self._vantage.modules:
                self._modules[module.id] = module
            log.info(f"Found {len(self._modules)} modules.")
        except Exception as e:
            log.error(f"Error building module table: {e}")
        log.info("Discovering loads...")
        await self._vantage.loads.initialize()
        log.info("Grouping loads by name to generate HA-style object_ids...")
        self._loads.clear()
        self._is_dimmable.clear()
        self._obj_id_map.clear()
        # Step 1: Group all loads by their slugified name
        grouped_loads: Dict[str, List[Any]] = defaultdict(list)
        for load in self._vantage.loads:
            # Store all loads
            self._loads[load.id] = load
            self._is_dimmable[load.id] = bool(getattr(load, "is_dimmable", True))
            # Group them for naming
            base_name = slugify(getattr(load, "name", "load"))
            grouped_loads[base_name].append(load)
        # Step 2: Iterate groups, sort by ID, and assign object_ids
        for base_name, loads_in_group in grouped_loads.items():
            # Sort by ID to ensure stable order (eg. light.fixture_2 is always the same light)
            loads_in_group.sort(key=lambda x: x.id)
            for i, load in enumerate(loads_in_group):
                if i == 0:
                    # First light gets the base name (e.g., "fixture")
                    object_id_base = base_name
                else:
                    # Subsequent lights get "_2", "_3", etc. (e.g., "fixture_2")
                    object_id_base = f"{base_name}_{i + 1}"
                # ==========================================================
                # <<< FIX 2 (Fan Conflict) START >>>
                # ==========================================================
                load_name_lower = (load.name or "").lower()
                if "fan" in load_name_lower:
                    # Append '_load' to avoid object_id collision with the fan template
                    object_id = f"{object_id_base}_load"
                    log_msg_suffix = f" (renamed to {object_id} to avoid fan template conflict)"
                else:
                    object_id = object_id_base
                    log_msg_suffix = ""
                # ==========================================================
                # <<< FIX 2 (Fan Conflict) END >>>
                # ==========================================================
                self._obj_id_map[load.id] = object_id
                log.info(f" -> Mapped Vantage ID {load.id} ({load.name}) to object_id: {object_id}{log_msg_suffix}")
        log.info(f"Load discovery and naming complete ({len(self._loads)} loads).")
        # Step 3: Publish discovery, attributes, and initial state
        for load_obj in self._loads.values():
            await self._publish_discovery_for_load_async(load_obj)
            await self._publish_attributes_for_load_async(load_obj)
            level = load_obj.level if load_obj.level is not None else 0.0
            await self._publish_load_state_async(load_obj.id, level)
    def _subscribe_to_load_events(self):
        if not self._vantage:
            return
        async def _cb_flex(event=None, load=None, data=None, *args, **kwargs):
            """Flexible event handler; prefers explicit kwargs, falls back to args."""
            self._last_event_time = time.monotonic()
            if load is None:
                for a in args:
                    if hasattr(a, "id") and hasattr(a, "level"):
                        load = a
                        break
            if load is None:
                load = kwargs.get("load")
            level = getattr(load, "level", None) if load is not None else None
            if level is None and isinstance(data, dict):
                level = data.get("level", data.get("value"))
            if load is None or level is None:
                log.debug("Event handler skipped: load or level is None.")
                return
            try:
                log.debug(f"Event received for Load {load.id} ({load.name}): Level={level}")
                await self._publish_load_state_async(load.id, float(level))
            except Exception as e:
                log.error(f"Event handler failed for load {load.id}: {e}", exc_info=True)
        try:
            self._vantage.loads.subscribe(
                callback=_cb_flex,
                event_type="state_change"
            )
            log.info("Subscribed to load change events.")
        except Exception as e:
            log.warning(f"Could not subscribe to load events ({e}); will rely on polling.")
    async def _set_level(self, load_id: int, level: float):
        if not self._vantage:
            log.warning(f"Cannot send command; Vantage not connected (load {load_id}).")
            return
        try:
            load = self._loads.get(load_id) or await self._vantage.loads.aget(load_id)
            if load is None:
                log.warning(f"Unknown load id {load_id}")
                return
            level = max(0.0, min(100.0, float(level)))
            log.info(f"VANTAGE API: Setting load {load_id} ({getattr(load, 'name', load_id)}) to {level:.1f}%")
           
            # FIX 9: If level is 0, call turn_off(), otherwise set_level()
            if level == 0.0:
                await load.turn_off()
            else:
                await load.set_level(level)
            # ==========================================================
            # <<< FIX 5 (Optimistic State) START >>>
            # Publish the new state immediately for a responsive UI
            await self._publish_load_state_async(load_id, level)
            # <<< FIX 5 (Optimistic State) END >>>
            # ==========================================================
        except Exception as e:
            log.error(f"Error setting level for {load_id}: {e}", exc_info=True)
    async def _publish_bridge_offline_async(self):
        """Publish 'offline' even if main MQTT session is down."""
        try:
            will = Will(AVAILABILITY_TOPIC, b"offline", qos=1, retain=True)
            async with Client(
                hostname=MQTT_HOST,
                port=MQTT_PORT,
                username=MQTT_USERNAME,
                password=MQTT_PASSWORD,
                will=will,
            ) as client:
                await client.publish(AVAILABILITY_TOPIC, "offline", qos=1, retain=True)
            log.info("Published bridge 'offline' status.")
        except Exception:
            log.debug("Could not publish 'offline' (broker likely down).")
    # ───────────── HA Discovery & State ─────────────
    async def _publish_all_discovery_async(self):
        for load_obj in self._loads.values():
            await self._publish_discovery_for_load_async(load_obj)
            await self._publish_attributes_for_load_async(load_obj)
    async def _publish_discovery_for_load_async(self, load: Any):
        if not self._mqtt_connected:
            return
        # Get the friendly name
        friendly_name = getattr(load, "name", f"Load {load.id}")
        # Get the HA-style object_id (e.g., "fixture_7") from our map
        object_id = self._obj_id_map.get(load.id)
        if not object_id:
            log.error(f"FATAL: No object_id found for load {load.id}. This should not happen.")
            return
        state_topic = self._topic("light", load.id, "state")
        cmd_topic = self._topic("light", load.id, "set")
        bri_state_topic = self._topic("light", load.id, "brightness", "state")
        bri_cmd_topic = self._topic("light", load.id, "brightness", "set")
        attr_topic = self._topic("light", load.id, "attributes")
        is_dim = self._is_dimmable.get(load.id, True)
        # ==========================================================
        # <<< FIX 11 (on/off fix) START >>>
        # ==========================================================
       
        # Base payload for all lights
        payload = {
            "name": friendly_name,
            "object_id": object_id,
            "unique_id": f"vantage_{VANTAGE_HOST}_load_{load.id}",
            "json_attributes_topic": attr_topic,
            "availability_topic": AVAILABILITY_TOPIC,
            "payload_available": "online",
            "payload_not_available": "offline",
            "state_topic": state_topic, # All lights report ON/OFF state
            "command_topic": cmd_topic, # Use /set for on/off commands
            "payload_on": "ON",
            "payload_off": "OFF",
            "device": {
                "identifiers": [f"vantage_controller_{VANTAGE_HOST}"],
                "name": f"Vantage Controller ({VANTAGE_HOST})",
                "manufacturer": "Vantage",
                "model": "InFusion (SDK)",
            },
        }
        if is_dim:
            # Dimmable lights: Add brightness topics and on_command_type
            payload.update({
                "brightness_state_topic": bri_state_topic,
                "brightness_command_topic": bri_cmd_topic,
                "brightness_scale": 255,
                "on_command_type": "brightness", # HA sends brightness for ON
            })
       
        # ==========================================================
        # <<< FIX 11 (on/off fix) END >>>
        # ==========================================================
        cfg_topic = self._ha_config_topic("light", object_id)
        await self._publish_async(cfg_topic, json.dumps(payload), retain=True, qos=1)
    async def _publish_attributes_for_load_async(self, load: Any):
        """Publish the Area Name and other metadata as retained attributes."""
        load_name = getattr(load, "name", "load")
        # Helpful debug when LOG_LEVEL=DEBUG
        if log.isEnabledFor(logging.DEBUG):
            log.debug(
                "Load %s '%s': area=%r area_id=%r parent_id=%r",
                getattr(load, "id", None),
                load_name,
                getattr(load, "area", None),
                getattr(load, "area_id", None),
                getattr(load, "parent_id", None),
            )
        area_name: Optional[str] = None
        # 1) Direct 'area' attribute (preferred)
        load_area = getattr(load, "area", None)
        if load_area is not None:
            # If it's an Area object, grab its name
            candidate = getattr(load_area, "name", None)
            if candidate:
                area_name = candidate
            # If it's an ID, look it up in our table
            elif isinstance(load_area, int):
                area_name = self._area_names.get(load_area)
        # 2) Legacy / alt style: 'area_id' lookup
        if not area_name:
            load_area_id = getattr(load, "area_id", 0)
            if isinstance(load_area_id, int) and load_area_id:
                area_name = self._area_names.get(load_area_id)
        # 3) Parent module fallback: module.area / module.area_id
        if not area_name:
            parent_id = getattr(load, "parent_id", 0)
            parent_module = self._modules.get(parent_id)
            if parent_module:
                parent_area = getattr(parent_module, "area", None)
                if parent_area is not None:
                    candidate = getattr(parent_area, "name", None)
                    if candidate:
                        area_name = candidate
                else:
                    parent_area_id = getattr(parent_module, "area_id", 0)
                    if isinstance(parent_area_id, int) and parent_area_id:
                        area_name = self._area_names.get(parent_area_id)
        # 4) Final default
        if not area_name:
            area_name = "Unassigned"
        # Define the attributes payload
        attributes = {
            "vantage_area": area_name,
            "vantage_id": load.id,
            "vantage_name": load_name,
        }
        # Publish to the attributes topic
        attr_topic = self._topic("light", load.id, "attributes")
        await self._publish_async(attr_topic, json.dumps(attributes), retain=True, qos=1)
    async def _publish_load_state_async(self, load_id: int, level: Optional[float]):
        if level is None:
            log.debug(f"Skipping state publish for load {load_id}: Level is None.")
            return
       
        state_topic = self._topic("light", load_id, "state")
        bri_state_topic = self._topic("light", load_id, "brightness", "state")
        is_dim = self._is_dimmable.get(load_id, True)
        state = "ON" if level > 0 else "OFF"
        # Convert 0.0-100.0% level to 0-255 brightness for HA
        bri = str(int(round((float(level) / 100.0) * 255.0)))
        load_obj = self._loads.get(load_id)
        load_name = load_obj.name if load_obj else 'Unknown'
        log.debug(f"STATE PUB: Load {load_id} ({load_name}): State='{state}', Brightness={bri}")
       
        # Always publish ON/OFF state
        await self._publish_async(state_topic, state, retain=True, qos=0)
       
        # Only publish brightness for dimmable lights
        if is_dim:
            await self._publish_async(bri_state_topic, bri, retain=True, qos=0)
    # ───────────── Health & Polling ─────────────
    # --- INSTRUMENTATION ---
    async def _publish_diagnostics_async(self):
        """Gather and publish system and connection diagnostics metrics."""
        if not self._mqtt_connected:
            return
        # 1. System Process Metrics (using psutil)
        try:
            cpu_pct = self._process.cpu_percent(interval=None) # Get instantaneous CPU%
            mem_info = self._process.memory_info()
            mem_mb = round(mem_info.rss / (1024 * 1024), 2)
           
            await self._publish_async(self._topic("diagnostics", "cpu_usage_pct"), str(cpu_pct), retain=False)
            await self._publish_async(self._topic("diagnostics", "memory_usage_mb"), str(mem_mb), retain=False)
        except Exception as e:
            log.warning(f"Failed to gather process metrics: {e}")
        # 2. Bridge Uptime
        uptime_s = int(time.monotonic() - self._start_time)
        await self._publish_async(self._topic("diagnostics", "uptime_s"), str(uptime_s), retain=True)
        # 3. Vantage and Broker Connection Status
        vantage_conn_status = "online" if self._vantage else "offline"
        await self._publish_async(
            self._topic("diagnostics", "vantage_connection_status"),
            vantage_conn_status,
            retain=True
        )
        # 4. Total Publications
        await self._publish_async(
            self._topic("diagnostics", "messages_published_total"),
            str(self._total_publishes),
            retain=False
        )
        # 5. Entity Count
        await self._publish_async(
            self._topic("diagnostics", "entity_count"),
            str(len(self._loads)),
            retain=True
        )
       
        # 6. Last Event Time (Staleness Monitor)
        time_since_last_event = int(time.monotonic() - self._last_event_time)
        await self._publish_async(
            self._topic("diagnostics", "time_since_last_event_s"),
            str(time_since_last_event),
            retain=False
        )
    async def _health_check_loop(self):
        log.info("Starting health check loop.")
        while not self._shutdown_requested:
            await asyncio.sleep(HEALTH_CHECK_INTERVAL)
           
            # Existing check for a quiet Vantage stream
            if time.monotonic() - self._last_event_time > HEALTH_CHECK_INTERVAL * 2.5:
                log.warning("Vantage event stream has been quiet for a while.")
           
            if self._mqtt_connected:
                # Publish the main LWT topic as a heartbeat
                await self._publish_async(AVAILABILITY_TOPIC, "online", retain=True, qos=1)
               
                # Publish diagnostics metrics on the same interval
                await self._publish_diagnostics_async()
               
        log.info("Health check loop ended.")
    # --- INSTRUMENTATION ---
    async def _poll_loop(self):
        log.info("Starting efficient polling loop.")
        while not self._shutdown_requested:
            await asyncio.sleep(POLL_INTERVAL)
            try:
                if not self._vantage:
                    continue
                log.debug(f"Polling all {len(self._loads)} load states...")
                await self._vantage.loads.fetch_state()
                for load_obj in self._vantage.loads:
                    if load_obj.level is not None:
                        await self._publish_load_state_async(load_obj.id, float(load_obj.level))
            except Exception as e:
                log.warning(f"Polling loop error: {e}")
        log.info("Polling loop ended.")
    # ───────────── Run / Stop ─────────────
    async def run(self):
        log.info("Starting Vantage MQTT Bridge (v0.9.11)...")
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                self._loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(self.stop(s)))
            except NotImplementedError:
                pass
        # MQTT loop
        self._mqtt_task = self._loop.create_task(self._mqtt_loop())
        # Vantage connect loop
        while not self._shutdown_requested:
            try:
                log.info(f"Connecting to Vantage at {VANTAGE_HOST} ...")
                # --- INSTRUMENTATION ---
                vantage_conn_start_time = time.monotonic()
                async with Vantage(VANTAGE_HOST, VANTAGE_USER, VANTAGE_PASS) as vantage:
                    self._vantage_connect_time = time.monotonic() - vantage_conn_start_time # Calculated latency
                # --- INSTRUMENTATION ---
                    log.info("Vantage connected.")
                    self._vantage = vantage
                    self._last_event_time = time.monotonic()
                    # Initialize areas and modules for attribute logic
                    log.info("Initializing Areas...")
                    await vantage.areas.initialize()
                    log.info("Area initialization complete.")
                    log.info("Initializing Modules...")
                    await vantage.modules.initialize()
                    log.info("Module initialization complete.")
                    await self._discover_loads()
                    self._subscribe_to_load_events()
                    if not self._health_task or self._health_task.done():
                        self._health_task = self._loop.create_task(self._health_check_loop())
                    if ENABLE_FALLBACK_POLLING and (not self._poll_task or self._poll_task.done()):
                        self._poll_task = self._loop.create_task(self._poll_loop())
                    if self._mqtt_connected:
                        await self._publish_async(AVAILABILITY_TOPIC, "online", retain=True, qos=1)
                    while not self._shutdown_requested:
                        await asyncio.sleep(1)
            except asyncio.CancelledError:
                log.info("Vantage connection task cancelled.")
                break
            except Exception as e:
                log.error(f"Vantage connection error: {e}", exc_info=True)
                self._vantage = None
                # ==========================================================
                # <<< FIX 1 (Flickering) START >>>
                # We no longer publish offline on a temp Vantage error
                # await self._publish_bridge_offline_async()
                # <<< FIX 1 (Flickering) END >>>
                # ==========================================================
                if not self._shutdown_requested:
                    log.info("Waiting 15s before retrying Vantage connection...")
                    await asyncio.sleep(15)
    async def stop(self, sig=None):
        if self._shutdown_requested:
            return
        self._shutdown_requested = True
        log.info(f"Shutdown requested (signal={getattr(sig, 'name', None)}).")
        # cancel tasks
        for task in (self._health_task, self._poll_task, self._mqtt_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        # offline status
        await self._publish_bridge_offline_async()
        # Vantage will close on context exit in run()
        self._vantage = None
        self._mqtt_client = None
        log.info("Bridge stopped.")
# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
async def main():
    bridge = VantageBridge()
    try:
        await bridge.run()
    finally:
        if not bridge._shutdown_requested:
            await bridge.stop()
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
