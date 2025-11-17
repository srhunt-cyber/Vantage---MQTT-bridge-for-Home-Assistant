#!/usr/bin/env python3
"""
Vantage <-> MQTT bridge
Version 0.9.22-final (Stable Merge)

- BASE: Uses v0.9.16 ("Grok") logic for dimming and restore,
  which manually tracks `_last_non_zero_level`. This is
  the confirmed working logic.
- MERGE 1 (v0.9.17): Replaced deprecated 'object_id'
  in _publish_bridge_device_async with the "dummy sensor"
  and `default_entity_id` fix to clear HA deprecation logs.
- MERGE 2 (v0.9.15): Re-added legacy unique_id logic to
  _publish_discovery_for_load_async to prevent entity duplication.
- MERGE 3 (Cleanup): Removed all hidden non-breaking space
  characters and added robust VANTAGE_HOST check.
- NOTE: Gamma correction has been *removed* as it was
  the source of the dimming bug.

DIMMING / RESTORE INVARIANTS (high level, see comments near methods):
- HA always talks brightness 0–255.
- We convert 0–255 <-> 0–100% linearly (no gamma).
- `_last_non_zero_level[load_id]` remembers the last non-zero level.
- OFF (level 0) never clears `_last_non_zero_level`.
- ON / brightness "ON" restores the last non-zero level, or 100% if none.
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
import psutil  # REQUIRED FOR METRICS
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
logging.getLogger("aiovantage").setLevel(logging.INFO)  # set DEBUG for protocol chatter

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────
VANTAGE_HOST = os.getenv("VANTAGE_HOST")  # Required; no default
if not VANTAGE_HOST:
    raise ValueError("VANTAGE_HOST must be set in .env")

VANTAGE_HOST_SAFE = VANTAGE_HOST.replace(".", "_")  # Sanitize for IDs
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

# Timing
RECONNECT_DELAY_MQTT = 10
HEALTH_CHECK_INTERVAL = 30  # Interval for sending metrics and heartbeat
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


def ha_to_vantage_level(brightness: int) -> float:
    """
    Convert HA 0–255 brightness to Vantage 0–100% (linear).
    This mirrors the v0.9.16 behavior exactly.
    """
    try:
        bri = int(brightness)
    except (TypeError, ValueError):
        bri = 0
    bri = max(0, min(255, bri))
    return (bri / 255.0) * 100.0


def vantage_to_ha_brightness(level: float) -> int:
    """
    Convert Vantage 0–100% to HA 0–255 brightness (linear).
    This mirrors the v0.9.16 behavior exactly.
    """
    try:
        lvl = float(level)
    except (TypeError, ValueError):
        lvl = 0.0
    lvl = max(0.0, min(100.0, lvl))
    return int(round((lvl / 100.0) * 255.0))


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
        self._loads: Dict[int, Any] = {}  # id -> load
        self._is_dimmable: Dict[int, bool] = {}  # id -> bool
        self._obj_id_map: Dict[int, str] = {}  # id -> object_id (e.g., "fixture_7")
        self._area_names: Dict[int, str] = {}  # id -> area name
        self._modules: Dict[int, Any] = {}  # id -> module object

        # BASE: "Grok" v0.9.16 logic
        # IMPORTANT: `_last_non_zero_level` underpins the "restore last brightness"
        # behavior. See DIMMING / RESTORE INVARIANTS at the top of the file.
        self._last_non_zero_level: Dict[int, float] = {}  # Track last non-zero level per load

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

    async def _publish_async(self, topic: str, payload: str, retain: bool = False, qos: int = 0):
        """
        Unified helper to publish MQTT messages and increment diagnostics counter.

        Behavior:
        - If MQTT is not connected, this is a safe no-op.
        """
        if self._mqtt_client and self._mqtt_connected:
            try:
                await self._mqtt_client.publish(topic, payload, qos=qos, retain=retain)
                self._total_publishes += 1  # --- INSTRUMENTATION ---
            except Exception as e:
                log.error(f"MQTT publish error on {topic}: {e}")

    # BASE: "Grok" v0.9.16 logic
    async def _handle_mqtt_message_async(self, message: Message):
        """
        Handle incoming MQTT commands:

        - BASE_TOPIC/light/<load_id>/brightness/set
        - BASE_TOPIC/light/<load_id>/set

        DIMMING / RESTORE BEHAVIOR (must stay in sync with _publish_load_state_async):
        - Numeric brightness 0–255 is mapped linearly to 0–100%.
        - Brightness "ON" restores last non-zero level (or 100% if none).
        - Brightness "OFF" sets level to 0 but does NOT clear last_non_zero_level.
        - ON/OFF topic uses the same restore semantics.
        """
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

                log.debug(f"Handling command for Load {load_id} ({load_obj.name}): Topic={topic}, Payload='{payload}'")

                # 1. Brightness Command (e.g. .../brightness/set)
                if len(parts) >= 5 and parts[3] == "brightness" and parts[4] == "set":
                    level = 0.0  # Default to 0
                    try:
                        # First, try to parse it as a number (0-255)
                        bri = int(payload)
                        # Use simple linear conversion (as in v0.9.16)
                        level = ha_to_vantage_level(bri)
                        log.info(
                            f"BRIGHTNESS CMD: Load {load_id} ({load_obj.name}): "
                            f"HA Brightness {bri} -> Vantage Level {level:.1f}%"
                        )
                        if level > 0:
                            # Remember last non-zero for future restores.
                            self._last_non_zero_level[load_id] = level

                    except ValueError:
                        # If it's not a number, it's probably "ON" or "OFF"
                        command = payload.upper()
                        if command == "OFF":
                            log.info(
                                f"BRIGHTNESS CMD (OFF): Load {load_id} ({load_obj.name}) "
                                f"received 'OFF'. Setting level 0.0%"
                            )
                            level = 0.0
                            # NOTE: We deliberately do NOT clear _last_non_zero_level here,
                            # so a subsequent ON can restore the old level.
                        elif command == "ON":
                            # Restore last non-zero level
                            level = self._last_non_zero_level.get(load_id, 100.0)
                            log.info(
                                f"BRIGHTNESS CMD (ON): Load {load_id} ({load_obj.name}) "
                                f"received 'ON'. Restoring level {level:.1f}%"
                            )
                        else:
                            log.warning(
                                f"BRIGHTNESS CMD (INVALID): Load {load_id} received invalid payload: '{payload}'. Ignoring."
                            )
                            return

                    await self._set_level(load_id, level, load_obj)
                    return

                # 2. ON/OFF Command (e.g. .../set)
                if len(parts) == 4 and parts[3] == "set":
                    command = payload.upper()
                    if command == "ON":
                        # Restore last non-zero level
                        level = self._last_non_zero_level.get(load_id, 100.0)
                        log.info(
                            f"ON/OFF CMD: Turning Load {load_id} ({load_obj.name}) ON to level {level:.1f}%."
                        )
                        await self._set_level(load_id, level, load_obj)
                        return
                    elif command == "OFF":
                        log.info(f"ON/OFF CMD: Turning Load {load_id} ({load_obj.name}) OFF.")
                        await load_obj.turn_off()
                        await self._publish_load_state_async(load_id, 0.0)  # Optimistic OFF state
                        return

        except Exception as e:
            log.error(f"Error parsing MQTT cmd {topic}='{payload}': {e}", exc_info=True)

    # ───────────── Vantage ─────────────
    def _get_area_name_for_load(self, load: Any) -> str:
        """Helper to find the area name for a given load object."""
        area_name: Optional[str] = None

        # 1) Direct 'area' attribute (preferred)
        load_area = getattr(load, "area", None)
        if load_area is not None:
            if hasattr(load_area, "name") and load_area.name:
                area_name = load_area.name
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
                    if hasattr(parent_area, "name") and parent_area.name:
                        area_name = parent_area.name
                else:
                    parent_area_id = getattr(parent_module, "area_id", 0)
                    if isinstance(parent_area_id, int) and parent_area_id:
                        area_name = self._area_names.get(parent_area_id)

        # 4) Final default
        if not area_name:
            area_name = "Unassigned"

        return area_name

    # BASE: "Grok" v0.9.16 logic
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
        self._last_non_zero_level.clear()  # Reset on rediscovery

        # Step 1: Group all loads by their slugified name
        grouped_loads: Dict[str, List[Any]] = defaultdict(list)
        for load in self._vantage.loads:
            # Store all loads
            self._loads[load.id] = load
            self._is_dimmable[load.id] = bool(getattr(load, "is_dimmable", True))

            # Initialize last_non_zero if on and >0
            if load.level is not None and load.level > 0:
                self._last_non_zero_level[load.id] = load.level

            # Group them for naming
            base_name = slugify(getattr(load, "name", "load"))
            grouped_loads[base_name].append(load)

        # Step 2: Iterate groups, sort by ID, and assign object_ids
        #
        # IMPORTANT:
        # This naming pattern is now STABLE in the user's live HA instance.
        # It allowed this bridge to replace a previous integration without
        # changing entity IDs for 111 lights. Do NOT change lightly.
        for base_name, loads_in_group in grouped_loads.items():
            # Sort by ID to ensure stable order (eg. light.fixture_2 is always the same light)
            loads_in_group.sort(key=lambda x: x.id)
            for i, load in enumerate(loads_in_group):
                if i == 0:
                    object_id_base = base_name
                else:
                    object_id_base = f"{base_name}_{i + 1}"

                load_name_lower = (load.name or "").lower()
                if "fan" in load_name_lower:
                    object_id = f"{object_id_base}_load"
                    log_msg_suffix = f" (renamed to {object_id} to avoid fan template conflict)"
                else:
                    object_id = object_id_base
                    log_msg_suffix = ""

                self._obj_id_map[load.id] = object_id
                log.info(
                    f" -> Mapped Vantage ID {load.id} ({load.name}) to object_id: {object_id}{log_msg_suffix}"
                )

        log.info(f"Load discovery and naming complete ({len(self._loads)} loads).")

        await self._publish_bridge_device_async()

        # Step 3: Publish discovery, attributes, and initial state
        for load_obj in self._loads.values():
            await self._publish_discovery_for_load_async(load_obj)
            await self._publish_attributes_for_load_async(load_obj)
            level = load_obj.level if load_obj.level is not None else 0.0
            await self._publish_load_state_async(load_obj.id, level)

    async def _handle_load_event(self, event=None, load=None, data=None, *args, **kwargs):
        """
        Flexible event handler; prefers explicit kwargs, falls back to args.

        This preserves the original v0.9.16 behavior:
        - Any non-zero level updates _last_non_zero_level[load.id].
        - Publishes the new state to MQTT.
        """
        self._last_event_time = time.monotonic()

        # Try positional args for a load
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
            if level > 0:
                # Any non-zero event remembers this as the last non-zero level
                self._last_non_zero_level[load.id] = float(level)
            await self._publish_load_state_async(load.id, float(level))
        except Exception as e:
            log.error(f"Event handler failed for load {load.id}: {e}", exc_info=True)

    # BASE: "Grok" v0.9.16 logic
    def _subscribe_to_load_events(self):
        if not self._vantage:
            return
        try:
            self._vantage.loads.subscribe(
                callback=self._handle_load_event,
                event_type="state_change",
            )
            log.info("Subscribed to load change events.")
        except Exception as e:
            log.warning(f"Could not subscribe to load events ({e}); will rely on polling.")

    # BASE: "Grok" v0.9.16 logic
    async def _set_level(self, load_id: int, level: float, load: Optional[Any] = None):
        """
        Set the level for a load. This SINGLE command should turn on AND dim.

        Behavior:
        - Clamps level to [0.0, 100.0].
        - Uses load.set_level(level) for both ON and dim.
        - Updates _last_non_zero_level[load_id] for any level > 0.
        - Publishes optimistic state to MQTT immediately.
        """
        if not self._vantage:
            log.warning(f"Cannot send command; Vantage not connected (load {load_id}).")
            return
        try:
            if load is None:
                load = self._loads.get(load_id) or await self._vantage.loads.aget(load_id)
            if load is None:
                log.warning(f"Unknown load id {load_id}")
                return

            level = max(0.0, min(100.0, float(level)))
            log.info(f"VANTAGE API: Setting load {load_id} ({load.name}) to {level:.1f}%")

            # This is the simplified, atomic command that works
            await load.set_level(level)

            if level > 0:
                # Remember last non-zero level for ON restore behavior.
                self._last_non_zero_level[load_id] = level

            # Optimistic update
            await self._publish_load_state_async(load_id, level)
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

    # MERGED: v0.9.17 Deprecation Fix
    async def _publish_bridge_device_async(self):
        """Publishes the main bridge device config to HA by creating a dummy sensor."""
        try:
            # We create a dummy "status" sensor.
            # This entity's config will contain the *real* device info.
            object_id = f"{BRIDGE_DEVICE_ID}_status"  # BRIDGE_DEVICE_ID is already safe
            topic = self._ha_config_topic("sensor", object_id)  # Uses the standard topic format
            payload = {
                "name": "Bridge Status",
                "default_entity_id": f"sensor.{object_id}",  # <-- THIS IS THE FIX
                "unique_id": f"{BRIDGE_DEVICE_ID}_status_sensor",
                "state_topic": AVAILABILITY_TOPIC,  # Re-use the availability topic
                "icon": "mdi:bridge",
                "device": {
                    "identifiers": [BRIDGE_DEVICE_ID],
                    "name": f"Vantage Controller ({VANTAGE_HOST})",  # Friendly name
                    "manufacturer": "Vantage",
                    "model": "InFusion (SDK) Bridge",
                    "sw_version": "0.9.22-final",  # Updated version
                },
                # This sensor will be part of the main device
                "entity_category": "diagnostic",
            }
            await self._publish_async(topic, json.dumps(payload), retain=True, qos=1)
            log.info("Published main bridge device (via dummy sensor) to Home Assistant.")
        except Exception as e:
            log.error(f"Failed to publish bridge device: {e}", exc_info=True)

    async def _publish_all_discovery_async(self):
        # Publish the bridge status sensor (which includes the device)
        await self._publish_bridge_device_async()
        # Now publish all lights and link them
        for load_obj in self._loads.values():
            await self._publish_discovery_for_load_async(load_obj)
            await self._publish_attributes_for_load_async(load_obj)

    # MERGED: v0.9.15 Legacy ID Fix
    async def _publish_discovery_for_load_async(self, load: Any):
        """
        Publish HA discovery for a single load.

        ENTITY IDENTITY / MIGRATION:
        - unique_id uses a LEGACY format including VANTAGE_HOST with dots:
            vantage_<VANTAGE_HOST>_load_<load_id>_light
          so HA treats these as the same entities as the previous bridge.
        - device.identifiers uses a SAFE ID with underscores (VANTAGE_HOST_SAFE)
          which only affects device grouping in HA, not the entity identity.
        """
        if not self._mqtt_connected:
            return

        friendly_name = getattr(load, "name", f"Load {load.id}")
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
        area_name = self._get_area_name_for_load(load)

        # This is the NEW, SAFE ID for the *device* itself (uses underscores)
        safe_load_device_id = f"vantage_{VANTAGE_HOST_SAFE}_load_{load.id}"

        # This is the OLD, LEGACY ID for the *entity* (uses periods)
        legacy_entity_unique_id = f"vantage_{VANTAGE_HOST}_load_{load.id}_light"

        payload = {
            "name": friendly_name,
            "unique_id": legacy_entity_unique_id,  # <-- USE THE LEGACY ID
            "json_attributes_topic": attr_topic,
            "availability_topic": AVAILABILITY_TOPIC,
            "payload_available": "online",
            "payload_not_available": "offline",
            "state_topic": state_topic,
            "command_topic": cmd_topic,
            "payload_on": "ON",
            "payload_off": "OFF",
            "device": {
                "identifiers": [safe_load_device_id],  # <-- USE THE SAFE ID
                "name": friendly_name,  # The device name, e.g., "Living Room Fixture"
                "suggested_area": area_name,
                "manufacturer": "Vantage",
                "model": "InFusion Load",
                "via_device": BRIDGE_DEVICE_ID,  # Links to the main bridge
            },
        }

        if is_dim:
            payload.update(
                {
                    "brightness_state_topic": bri_state_topic,
                    "brightness_command_topic": bri_cmd_topic,
                    "brightness_scale": 255,
                }
            )

        cfg_topic = self._ha_config_topic("light", object_id)
        await self._publish_async(cfg_topic, json.dumps(payload), retain=True, qos=1)

    async def _publish_attributes_for_load_async(self, load: Any):
        """Publish the Area Name and other metadata as retained attributes."""
        load_name = getattr(load, "name", "load")
        area_name = self._get_area_name_for_load(load)
        attributes = {
            "vantage_area": area_name,  # Still useful for display
            "vantage_id": load.id,
            "vantage_name": load_name,
        }
        attr_topic = self._topic("light", load.id, "attributes")
        await self._publish_async(attr_topic, json.dumps(attributes), retain=True, qos=1)

    # BASE: "Grok" v0.9.16 logic
    async def _publish_load_state_async(self, load_id: int, level: Optional[float]):
        """
        Publish current load state and brightness to MQTT.

        MUST stay in sync with _handle_mqtt_message_async mapping logic:
        - Level > 0 => state "ON"
        - Level <= 0 => state "OFF"
        - Brightness is derived linearly from 0–100% -> 0–255.
        """
        if level is None:
            log.debug(f"Skipping state publish for load {load_id}: Level is None.")
            return

        state_topic = self._topic("light", load_id, "state")
        bri_state_topic = self._topic("light", load_id, "brightness", "state")
        is_dim = self._is_dimmable.get(load_id, True)
        state = "ON" if level > 0 else "OFF"

        bri = str(vantage_to_ha_brightness(level))

        load_obj = self._loads.get(load_id)
        load_name = load_obj.name if load_obj else "Unknown"
        log.debug(
            f"STATE PUB: Load {load_id} ({load_name}): State='{state}', Brightness={bri} (Vantage Level={level:.1f}%)"
        )

        await self._publish_async(state_topic, state, retain=False, qos=0)

        if is_dim:
            await self._publish_async(bri_state_topic, bri, retain=False, qos=0)

    # ───────────── Health & Polling ─────────────
    # --- INSTRUMENTATION ---
    async def _publish_diagnostics_async(self):
        """Gather and publish system and connection diagnostics metrics."""
        if not self._mqtt_connected:
            return
        try:
            cpu_pct = self._process.cpu_percent(interval=None)  # Get instantaneous CPU%
            mem_info = self._process.memory_info()
            mem_mb = round(mem_info.rss / (1024 * 1024), 2)
            await self._publish_async(self._topic("diagnostics", "cpu_usage_pct"), str(cpu_pct), retain=False)
            await self._publish_async(self._topic("diagnostics", "memory_usage_mb"), str(mem_mb), retain=False)
        except Exception as e:
            log.warning(f"Failed to gather process metrics: {e}")

        uptime_s = int(time.monotonic() - self._start_time)
        await self._publish_async(self._topic("diagnostics", "uptime_s"), str(uptime_s), retain=True)

        vantage_conn_status = "online" if self._vantage else "offline"
        await self._publish_async(
            self._topic("diagnostics", "vantage_connection_status"),
            vantage_conn_status,
            retain=True,
        )

        await self._publish_async(
            self._topic("diagnostics", "messages_published_total"),
            str(self._total_publishes),
            retain=False,
        )

        await self._publish_async(
            self._topic("diagnostics", "entity_count"),
            str(len(self._loads)),
            retain=True,
        )

        time_since_last_event = int(time.monotonic() - self._last_event_time)
        await self._publish_async(
            self._topic("diagnostics", "time_since_last_event_s"),
            str(time_since_last_event),
            retain=False,
        )

    async def _health_check_loop(self):
        log.info("Starting health check loop.")
        while not self._shutdown_requested:
            await asyncio.sleep(HEALTH_CHECK_INTERVAL)

            if time.monotonic() - self._last_event_time > HEALTH_CHECK_INTERVAL * 2.5:
                log.warning("Vantage event stream has been quiet for a while.")

            if self._mqtt_connected:
                await self._publish_async(AVAILABILITY_TOPIC, "online", retain=True, qos=1)
                await self._publish_diagnostics_async()

        log.info("Health check loop ended.")

    # --- INSTRUMENTATION ---
    # BASE: "Grok" v0.9.16 logic
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
                        level = float(load_obj.level)
                        if level > 0:
                            self._last_non_zero_level[load_obj.id] = level  # Update on poll
                        await self._publish_load_state_async(load_obj.id, level)
            except Exception as e:
                log.warning(f"Polling loop error: {e}")

        log.info("Polling loop ended.")

    # ───────────── Run / Stop ─────────────
    async def run(self):
        log.info("Starting Vantage MQTT Bridge (v0.9.22-final)...")

        # Signal handlers
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                self._loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(self.stop(s)))
            except NotImplementedError:
                # Not available on some platforms (e.g. Windows)
                pass

        # MQTT loop
        self._mqtt_task = self._loop.create_task(self._mqtt_loop())

        # Vantage connect loop
        while not self._shutdown_requested:
            try:
                log.info(f"Connecting to Vantage at {VANTAGE_HOST} ...")
                vantage_conn_start_time = time.monotonic()
                async with Vantage(VANTAGE_HOST, VANTAGE_USER, VANTAGE_PASS) as vantage:
                    self._vantage_connect_time = time.monotonic() - vantage_conn_start_time
                    log.info("Vantage connected.")
                    self._vantage = vantage
                    self._last_event_time = time.monotonic()

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
            except (Exception, socket.gaierror) as e:
                log.error(f"Vantage connection error: {e}")
                self._vantage = None
                if not self._shutdown_requested:
                    log.info(f"Waiting {RECONNECT_DELAY_MQTT}s before retrying Vantage connection...")
                    await asyncio.sleep(RECONNECT_DELAY_MQTT)

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
