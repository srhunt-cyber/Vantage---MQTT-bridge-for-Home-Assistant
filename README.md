# Vantage (InFusion) MQTT Bridge for Home Assistant
**Version 1.1.0 (Sniper Edition)**

A standalone, high-reliability Python bridge that connects Legrand Vantage InFusion lighting controllers to Home Assistant via MQTT.

**Designed for stability, discovery, and seamless migration.**

---

## 🎯 Why this Bridge?

This project is an alternative to the standard [Home Assistant Vantage Integration](https://github.com/loopj/home-assistant-vantage). It runs as a **separate service** (decoupled from HA) and acts as a "traffic cop" for your legacy Vantage hardware.

It is engineered for three specific problems:
1.  **The Restart Problem:** If you restart Home Assistant frequently (to update configs), it drops the Telnet connection to Vantage. On older controllers, this often causes a lockup that requires a physical reboot. Since this bridge runs independently, you can restart HA 50 times a day and the Vantage connection stays alive.
2.  **The "Command Flood" Crash:** Legacy controllers often lock up ("buffer overflow") when Home Assistant sends too many commands at once (e.g., "Turn off house"). This bridge throttles traffic to prevent this.
3.  **"Scene Blindness":** Physical button presses often don't register in HA because the controller stays silent during macros. This bridge "spies" on the logs to catch every press.

---

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

---

## 📦 Installation

1.  **Clone the Repository:**
    ```bash
    git clone [https://github.com/srhunt-cyber/Vantage---MQTT-bridge-for-Home-Assistant.git](https://github.com/srhunt-cyber/Vantage---MQTT-bridge-for-Home-Assistant.git)
    cd Vantage---MQTT-bridge-for-Home-Assistant
    ```

2.  **Set up Environment:**
    ```bash
    python3 -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt
    ```

3.  **Configure:**
    Copy the template and edit your IP/MQTT details.
    ```bash
    cp .env.example .env
    nano .env
    ```

4.  **Run as Service (Recommended):**
    ```bash
    # Edit the provided service file with your paths/user
    nano vantage-bridge.service
    
    # Install
    sudo cp vantage-bridge.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now vantage-bridge.service
    ```

---

## ⚙️ Tuning & Configuration (.env)

The `.env` file puts you in control of the bridge's timing.

| Setting | Default | Description |
| :--- | :--- | :--- |
| `POLL_INTERVAL` | `90` | **Safety Heartbeat.** How often (in seconds) to force a full status check if the house is silent. |
| `POLL_QUIET_TIME` | `5` | **Smart Delay.** Prevents polling if a user is currently interacting with the system. Set this to your longest fade time + 1s. |
| `COMMAND_THROTTLE_DELAY` | `0.02` | **Crash Guard.** Sleep time (seconds) between outgoing commands. `0.02` (20ms) is invisible to the eye but prevents buffer overflows. |
| `PUBLISH_RAW_BUTTON_EVENTS` | `false` | **Debug Mode.** If true, floods MQTT with raw JSON for every button press. Useful for troubleshooting. |

---

## 🛠️ Cookbook: Advanced Automations

Since "Lazy Discovery" exposes physical button presses as events, you can do things that were previously impossible.

### Recipe 1: The "Double-Tap" Fixer
If a light ever gets out of sync (a "Zombie Light"), double-tap the wall switch to force a resync.
```yaml
trigger:
  - platform: device
    domain: mqtt
    device_id: vantage_kp_101  # Auto-discovered device
    type: button_short_press
    subtype: button_1
action:
  - wait_for_trigger:
      - platform: device
        domain: mqtt
        device_id: vantage_kp_101
        type: button_short_press
        subtype: button_1
    timeout: "00:00:01"
  - service: light.turn_off
    target: {entity_id: light.my_room}
  - delay: "00:00:01"
  - service: light.turn_on
    target: {entity_id: light.my_room}
Recipe 2: The "Smart Exit" Switch

Use a simple door button to turn ON the room if entering, but turn OFF the whole suite if leaving.

YAML
trigger:
  - platform: device
    domain: mqtt
    device_id: vantage_kp_50
    type: button_short_press
    subtype: button_1
condition:
  - condition: state
    entity_id: light.main_ceiling
    state: "on"
action:
  - service: light.turn_off
    target:
      entity_id: [light.main_ceiling, light.lamp_1, light.fan]
✨ Seamless Migration (Safe for Dashboards)
We know the pain of renaming entities. This bridge uses the exact same naming logic (slugify) as the official Vantage integration.

Drop-in Replacement: If your light is currently light.kitchen_overhead, this bridge will generate the exact same Entity ID.

Zero Work: Your existing dashboards, scripts, and automations will work instantly after switching.

Migration Steps:

Stop or remove the old integration.

Run this bridge script.

In Home Assistant, go to Developer Tools > YAML and click "Reload MQTT Entities".

Monitoring Dashboard
This repository includes YAML for a Home Assistant dashboard to monitor the bridge's health.

Add the contents of mqtt_sensors.yaml (if provided) or manually configure the diagnostics sensors.

Install the custom:gauge-card from HACS.

Create a new dashboard card to track CPU, Memory, and Uptime via the vantage/diagnostics/ topics.

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
loopj for the incredible aiovantage library.

srhunt-cyber for the Service/Bridge architecture.

MIT Licensed.