# Vantage (InFusion) MQTT Bridge for Home Assistant

**Version:** 1.1.0 (Sniper Edition)

A standalone, high-reliability Python bridge that connects **Legrand Vantage InFusion** lighting controllers to **Home Assistant** via **MQTT**.

Built to be stable under real-world “house-wide” automations and to keep state accurate even when the Vantage system executes scenes/macros that don’t broadcast cleanly.

---

## Why this bridge?

This project is an alternative to the standard Home Assistant Vantage integration. It runs as a **separate service** (decoupled from HA) and acts as a “traffic cop” for legacy Vantage hardware.

It is engineered for three common reliability problems:

1. **The restart problem**  
   Restarting Home Assistant can drop the controller connection. On some older controllers this can lead to lockups requiring a reboot.

2. **Command-flood crashes**  
   Legacy controllers may choke when Home Assistant sends many commands at once (for example, “Turn off house”).

3. **Scene blindness / stale state**  
   Some controllers execute keypad macros/scenes but don’t reliably broadcast the resulting state changes.

---

## Key features

### 1) Instant load discovery (zero config)

On startup, the bridge queries the controller and discovers lights/switches/relays:

- Uses existing Vantage names
- Assigns devices to the Vantage “Area” (room) in Home Assistant
- Populates HA entities via MQTT discovery

### 2) “Lazy discovery” for keypad buttons (Log Tap)

Many systems don’t expose keypad button presses cleanly.  
This bridge watches the controller event log stream and **auto-creates** triggers the first time you press a keypad button.

**Result:** Walk up to a keypad, press a button once, and it appears in Home Assistant ready for automations.

### 3) “Sniper polling” (state accuracy without spam)

Instead of polling constantly:

- Detect a physical interaction
- Wait for fades/macros to complete (configurable)
- Poll once to sync state

### 4) Serial throttling (crash protection)

Home Assistant can fire MQTT commands extremely fast; some controllers can’t.  
A micro-throttle feeds commands safely to avoid controller overflow.

---

## Requirements

- Python 3.10+ (3.11+ recommended)
- MQTT broker (e.g., Mosquitto)
- Network access to your Vantage controller (InFusion)

---

## Quick start

### 1) Clone and install

```bash
git clone https://github.com/srhunt-cyber/Vantage---MQTT-bridge-for-Home-Assistant.git
cd Vantage---MQTT-bridge-for-Home-Assistant

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2) Configure

Copy the template and edit your settings:

```bash
cp .env.example .env
nano .env
```

### 3) Run

```bash
source .venv/bin/activate
python3 vantage_bridge.py
```

---

## Run as a systemd service (recommended)

Create `/etc/systemd/system/vantage-bridge.service`:

```ini
[Unit]
Description=Vantage MQTT Bridge
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/path/to/Vantage---MQTT-bridge-for-Home-Assistant
EnvironmentFile=/path/to/Vantage---MQTT-bridge-for-Home-Assistant/.env
ExecStart=/path/to/Vantage---MQTT-bridge-for-Home-Assistant/.venv/bin/python /path/to/Vantage---MQTT-bridge-for-Home-Assistant/vantage_bridge.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now vantage-bridge.service
sudo systemctl status vantage-bridge.service
```

Tips:

- Use an absolute path for `WorkingDirectory`, `.env`, and the Python interpreter.
- If you run into permissions issues, ensure `User=...` owns the repo folder (or use `Group=`).

---

## Configuration (.env)

See `.env.example` for the full list. The main timing knobs:

| Setting | Default | What it does |
|---|---:|---|
| `POLL_INTERVAL` | `90` | “Safety heartbeat” full check when the house is quiet |
| `POLL_QUIET_TIME` | `5` | Wait time after interaction before polling (set to your longest fade + ~1s) |
| `COMMAND_THROTTLE_DELAY` | `0.02` | Delay (seconds) between outgoing commands to prevent overflow |

Suggested starting points:

- If you have long fades/macros: increase `POLL_QUIET_TIME` (e.g., 6–10 seconds).
- If you ever see missed commands: slightly increase `COMMAND_THROTTLE_DELAY` (e.g., 0.03–0.06).
- If your system is very stable and quiet: increase `POLL_INTERVAL` to reduce chatter (e.g., 120–180).

---

## How keypad “Lazy Discovery” works (Log Tap)

Some InFusion setups don’t provide a clean way to subscribe to keypad button actions.  
This bridge listens to the controller’s event log stream and extracts keypad press events.

When a button is observed for the first time, the bridge:

- creates a corresponding MQTT-discovered entity/trigger in Home Assistant
- publishes future presses/releases for automations

Practical workflow:

1. Start the bridge
2. Press each keypad button you care about once
3. Build HA automations using the created button events

---

## Cookbook: advanced automations

Because “Lazy Discovery” exposes keypad presses as triggers, you can build higher-level behaviors.

### Recipe 1: “Double-tap fixer” (resync a zombie light)

If a light ever gets out of sync, double-tap the wall button to force a resync.

```yaml
alias: Vantage - Double Tap Resync Light
mode: single

trigger:
  - platform: device
    domain: mqtt
    device_id: vantage_kp_101
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
    target:
      entity_id: light.my_room
  - delay: "00:00:01"
  - service: light.turn_on
    target:
      entity_id: light.my_room
```

### Recipe 2: “Smart exit switch”

Use a door button to turn ON when entering, but turn OFF multiple lights if leaving.

```yaml
alias: Vantage - Smart Exit Switch
mode: restart

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
      entity_id:
        - light.main_ceiling
        - light.lamp_1
        - light.fan
```

---

## Seamless migration (safe for dashboards)

This bridge is intended to be a drop-in replacement for existing naming in many setups.

Migration steps:

1. Stop/disable the old integration (if applicable)
2. Start this bridge
3. In Home Assistant: **Settings → Devices & services → MQTT → Reload**

If you previously had entities with the same names:

- you may want to remove old entities first to avoid duplicates
- or change your MQTT discovery “prefix” (if your bridge supports it) during migration

---

## Optional: Monitoring sensors in Home Assistant

If you publish bridge diagnostics/status to MQTT, you can create HA MQTT sensors like:

```yaml
mqtt:
  sensor:
    - name: "Vantage Bridge Status"
      state_topic: "vantage/bridge/status"

    - name: "Vantage Bridge CPU"
      state_topic: "vantage/diagnostics/cpu_usage_pct"
      unit_of_measurement: "%"

    - name: "Vantage Bridge Memory"
      state_topic: "vantage/diagnostics/mem_usage_pct"
      unit_of_measurement: "%"
```

You can put this in your HA `configuration.yaml` (or a package), then reload MQTT.

---

## MQTT topic reference (examples)

These are example patterns (adjust if you changed your base topic).

### Loads (lights/switches)

- Load On/Off state:  
  `vantage/light/<id>/state` → `ON` / `OFF`

- Load brightness state (if dimmable):  
  `vantage/light/<id>/brightness/state` → `0–255`

- Load On/Off command:  
  `vantage/light/<id>/set` → `ON` / `OFF`

- Load brightness command:  
  `vantage/light/<id>/brightness/set` → `0–255`

### Keypads

- Button action:  
  `vantage/keypad/<id>/button/<pos>/action` → `press` / `release`

### Bridge / diagnostics

- Bridge status:  
  `vantage/bridge/status` → `online` / `offline`

- Diagnostics metrics:  
  `vantage/diagnostics/<metric>`  
  Examples: `cpu_usage_pct`, `mem_usage_pct`

---

## Troubleshooting

### The bridge starts but I don’t see entities in HA

- Confirm HA is connected to the same MQTT broker as the bridge
- In HA: **Settings → Devices & services → MQTT** and confirm discovery is enabled
- Check the broker for discovery topics (use `mosquitto_sub -t '#' -v` carefully)

### Keypads aren’t showing up

- Press each keypad button once after the bridge is running (that’s the “lazy discovery” trigger)
- Confirm your Vantage controller event log stream is accessible and enabled
- Increase log verbosity (if supported) to see whether keypad events are being detected

### Commands sometimes get ignored

- Increase `COMMAND_THROTTLE_DELAY` slightly (0.02 → 0.03–0.06)
- Avoid “blast” automations that toggle dozens of loads in the same millisecond

### State gets stale after running macros/scenes

- Increase `POLL_QUIET_TIME` to allow fades/macros to finish before the bridge polls
- Ensure the bridge is actually polling after interactions (check logs)

---

## Architectural note: “Log Tap” with aiovantage

This bridge uses the `aiovantage` library but attaches a custom logging handler to intercept the controller’s event log stream in order to detect keypad presses on systems that don’t expose them cleanly.

---

## Credits

- `loopj` for the `aiovantage` library and related Vantage tooling
