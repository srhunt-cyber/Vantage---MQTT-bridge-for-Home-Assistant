# Vantage (InFusion) MQTT Bridge for Home Assistant
**Version 1.1.0 (Sniper Edition)**

This is a standalone Python bridge that connects a Legrand Vantage InFusion lighting controller to Home Assistant via MQTT Auto-Discovery.

This project began as a solution to *real-world reliability problems*. After struggling with controller lockups, "command floods," and state desynchronization using standard integrations, this bridge was engineered to run as a separate, persistent service that acts as a traffic cop between modern Home Assistant (fast) and legacy Vantage hardware (slow).

---

## Why Choose This Bridge?

This project is an alternative—not a competitor—to the excellent [in-HA Vantage Integration](https://github.com/loopj/home-assistant-vantage). It is offered to the community for those who prefer a decoupled, service-based architecture.

This bridge is for those who:
* Need **maximum reliability**: It decouples Vantage from Home Assistant. Reboots, reloads, or upgrades in HA won’t affect your lighting bridge.
* Have experienced **controller flakiness** (buffer overflows) when sending too many commands at once.
* Suffer from **"Scene Blindness"** (where physical wall switches change the lights, but Home Assistant doesn't see the update).
* Prefer **MQTT-based infrastructure** for automation and debugging.

---

## 🚀 Key Features

## ✨ Key Features

### 1. Instant Load Discovery (Zero Config)
On startup, the bridge immediately queries your Vantage controller and discovers every light, switch, and relay.
* **Auto-Naming:** Uses your existing Vantage names (e.g., "Kitchen Overhead").
* **Auto-Area:** Reads the "Area" (Room) from Vantage and automatically assigns the device to that room in Home Assistant.
* **Result:** You start the bridge, and seconds later, your entire house is populated in Home Assistant. No YAML required.

### 2. "Lazy Discovery" for Buttons (The Log Tap)
Most integrations struggle to see Keypad Button presses because the controller doesn't broadcast them like it does for lights.
* **The Magic:** This bridge watches the debug logs in the background. When you physically press a button on a wall keypad, the bridge intercepts the event.
* **The Action:** If it hasn't seen that button before, it **automatically creates a Device Trigger**.
* **The Result:** To automate a scene button, just walk up to it, press it once, and it appears in Home Assistant ready for use.

### 3. "Sniper Polling" (State Accuracy)
Legacy Vantage controllers are often "Scene Blind"—they execute macros but don't tell Home Assistant the result.
* **The Old Way:** Poll every 5 seconds (floods the network, causes lag).
* **The Sniper Way:** The bridge detects that button press instantly, waits a configurable time (default 5s) for the fade to finish, and *then* polls exactly once.
* **Result:** Your app stays perfectly in sync without bogging down the network.

### 4. Serial Throttling (Crash Protection)
Modern Home Assistant systems can fire MQTT commands in milliseconds. Legacy Vantage serial buffers can often only handle ~3-4 commands per second before overflowing.
* **The Fix:** A configurable micro-throttle (Default: 20ms) manages the queue.
* **Result:** You can ask Alexa to "Turn off the House" (50+ lights) and the bridge will feed them to the controller safely, one by one, without a crash.

## Prerequisites

1.  Running Python scripts on a Linux host (like a Raspberry Pi, the Home Assistant VM, or a dedicated server).
2.  Managing a `systemd` service (recommended) to keep the script running.
3.  An MQTT broker (e.g., Mosquitto).

---

## Installation

1.  **Clone the Repository:**
    ```bash
    git clone [https://github.com/srhunt-cyber/Vantage---MQTT-bridge-for-Home-Assistant.git](https://github.com/srhunt-cyber/Vantage---MQTT-bridge-for-Home-Assistant.git)
    cd Vantage---MQTT-bridge-for-Home-Assistant
    ```

2.  **Set up a Python Environment:**
    ```bash
    python3 -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt
    ```

3.  **Configure:**
    Copy `.env.example` to `.env` and edit it with your Vantage and MQTT broker details. This file also contains the tuning knobs for the "Sniper" logic.

    ```bash
    cp .env.example .env
    nano .env
    ```

4.  **Run the Bridge (Test):**
    You can run it directly from your terminal to test.
    ```bash
    python vantage_bridge.py
    ```

5.  **Set up as a Service (Recommended):**
    A sample `vantage-bridge.service` file is included.
    
    ```bash
    # Edit the paths in the service file to match your setup
    # Make sure User, WorkingDirectory, and ExecStart are correct.
    nano vantage-bridge.service
    
    # Copy it to systemd
    sudo cp vantage-bridge.service /etc/systemd/system/
    
    # Enable and start the service
    sudo systemctl daemon-reload
    sudo systemctl enable vantage-bridge.service
    sudo systemctl start vantage-bridge.service
    
    # Check the status
    sudo systemctl status vantage-bridge.service
    ```

---

## ⚙️ Configuration & Tuning (.env)

The `.env` file controls the behavior of the bridge.

| Setting | Default | Description |
| :--- | :--- | :--- |
| `POLL_INTERVAL` | `90` | Safety poll in seconds. Forces a full status check if no activity is detected. |
| `POLL_QUIET_TIME` | `5` | **Crucial.** Prevents polling if the system was active recently. Set this to your longest fade time + 1 second. |
| `COMMAND_THROTTLE_DELAY` | `0.02` | Sleep time (seconds) between outgoing commands. `0.02` (20ms) prevents buffer overflows while remaining invisible to the user. |
| `PUBLISH_RAW_BUTTON_EVENTS` | `false` | If true, floods MQTT with raw JSON for every button press. Useful for finding Keypad IDs during setup. |

---

## ��️ Cookbook / Examples

Since this bridge exposes Keypad Button presses as MQTT events, you can create advanced automations in Home Assistant that were previously impossible.

### Recipe 1: The "Double-Tap" Fixer
Legacy systems sometimes get out of sync ("Zombie Lights"). This automation watches for a double-tap on a wall switch and forces a hard resync.

```yaml
- alias: "Fix Sync Mismatch (Double Tap)"
  mode: single
  trigger:
    - platform: mqtt
      topic: "vantage/keypad/101/button/1/action" # Replace with your Topic/ID
      payload: "press"
  action:
    # Wait to see if user presses again within 1 second
    - wait_for_trigger:
        - platform: mqtt
          topic: "vantage/keypad/101/button/1/action"
          payload: "press"
      timeout: "00:00:01"
      continue_on_timeout: false
    # Force Reset Sequence (Off then On)
    - service: light.turn_off
      target:
        entity_id: light.my_room_lights
    - delay: "00:00:01"
    - service: light.turn_on
      target:
        entity_id: light.my_room_lights
Recipe 2: The "Smart Exit" Switch

Use a door switch to act as a normal toggle when entering, but a "Room Off" switch when leaving.

YAML
- alias: "Smart Door Switch - All Off on Exit"
  mode: single
  trigger:
    - platform: mqtt
      topic: "vantage/keypad/50/button/1/action"
      payload: "press"
  condition:
    # Only run if the main light is ALREADY ON (We are leaving)
    - condition: state
      entity_id: light.main_ceiling
      state: "on"
  action:
    # Wait for Vantage to turn off the main light natively
    - delay: "00:00:00.5"
    # Clean up the rest of the room (lamps, fans, etc.)
    - service: light.turn_off
      target:
        entity_id:
          - light.lamp_1
          - light.fan_2
✨ Entity Naming and Migration
Migrating from the In-HA Vantage Integration?

This bridge was designed to be a drop-in replacement. It uses the same slugify naming logic as the popular Vantage InFusion (Asyncio) integration. This means your existing entity_ids (e.g., light.fixture_5) should be identical.

Migration Steps:

Stop or remove the old integration.

Run this bridge script.

In Home Assistant, go to Developer Tools > YAML and click "Reload MQTT Entities".

Automatic Area Assignment

This bridge creates a unique "device" in Home Assistant for each individual light. This allows the script to add the suggested_area property, so HA will automatically know which room (e.g., "Kitchen") each light belongs to.

Monitoring Dashboard
This repository includes YAML for a Home Assistant dashboard to monitor the bridge's health, as well as the mqtt/mqtt_sensors.yaml configuration to create the sensors.

Add the contents of mqtt_sensors.yaml to your Home Assistant MQTT configuration.

Install the custom:gauge-card from HACS (in the HACS "Frontend" store).

Create a new dashboard and use the YAML from lovelace_dashboard.yaml.

MQTT Topic Reference
Bridge Status: vantage/bridge/status ("online" / "offline")

Load State: vantage/light/<id>/state (Publishes "ON" / "OFF")

Load Brightness: vantage/light/<id>/brightness/state (Publishes 0-255)

Keypad Action: vantage/keypad/<id>/button/<pos>/action (Publishes "press" / "release")

On/Off Commands: vantage/light/<id>/set (Expects "ON" / "OFF")

Brightness Commands: vantage/light/<id>/brightness/set (Expects 0-255)

Bridge Diagnostics: vantage/diagnostics/<metric> (e.g., vantage/diagnostics/cpu_usage_pct)

🧠 Architectural Note regarding aiovantage
This bridge uses the aiovantage library but attaches a custom logging.Handler to intercept the EL: (Event Log) stream. This is necessary because the standard Vantage SDK does not expose button presses as subscribable events in many firmware versions. This "Log Tap" allows us to react to physical user interaction without modifying the core library.

Credits
This bridge is powered by the aiovantage library.

MIT Licensed.
