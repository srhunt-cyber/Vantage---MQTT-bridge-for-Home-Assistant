# Vantage (InFusion) MQTT Bridge for Home Assistant

**Version:** 1.1.0 (Sniper Edition)

A standalone, high-reliability Python bridge that connects **Legrand Vantage InFusion** lighting controllers to **Home Assistant** via **MQTT**.

Designed for stability in real homes: controller-safe command pacing, reliable state sync after scenes/fades, and “lazy discovery” of keypad presses via the controller event log stream.

---

## What this bridge solves

This project runs as a **separate service** (decoupled from Home Assistant) and is engineered for three common reliability problems:

1. **Restart resilience**  
   Home Assistant restarts shouldn’t destabilize a legacy controller connection.

2. **Command-flood protection**  
   Legacy controllers can choke when many commands arrive at once (e.g., “Turn off house”).

3. **Scene blindness / stale state**  
   Some controllers execute keypad macros/scenes but don’t reliably broadcast resulting load state changes.

---

## Key features

### 1) Instant load discovery (zero config)
On startup, the bridge queries the controller and discovers loads:
- Uses existing Vantage load names
- Assigns devices to the Vantage “Area” (room) in Home Assistant
- Publishes entities via MQTT discovery

### 2) “Lazy discovery” for keypad buttons (Log Tap)

Some systems don’t expose keypad button presses cleanly.  
This bridge watches the controller’s **event log** stream and auto-creates triggers the first time you press a keypad button.

**Workflow:** start the bridge → press the buttons you care about once → build HA automations.

#### Keypads as “scene triggers” for mixed ecosystems (Vantage + low-cost smart devices)

A major motivation for recognizing keypad presses/macros is that many homes have **non‑Vantage loads** in the same spaces (for example, lamps or accent lighting on TP‑Link/Kasa plugs/switches).

Once keypad actions are exposed to Home Assistant, a Vantage keypad effectively becomes a **universal scene controller**:

- Press a Vantage keypad button → HA automation triggers
- HA automation updates **both** Vantage loads **and** non‑Vantage devices (Kasa/TP‑Link, Hue, etc.)
- The room behaves like one coherent lighting scene even if some devices aren’t on the Vantage bus

This lets you “fill in” lighting coverage without rewiring or adding expensive Vantage modules, while keeping the same wall keypads and user experience.

### 3) “Sniper polling” (state accuracy without spam)
Instead of constant polling:
- Detect a physical interaction (keypad press)
- Wait for fades/macros to complete
- Poll once to sync state

### 4) Serial throttling (crash protection)
Adds micro-delays between outgoing commands to prevent controller buffer overflows.

---

## Requirements

- Python 3.10+ (3.11+ recommended)
- MQTT broker (e.g., Mosquitto)
- IP reachability from this service to your Vantage controller (InFusion)

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

---

## Configuration reference (.env)

### Required
| Setting | Example | Description |
|---|---|---|
| `VANTAGE_HOST` | `192.168.1.50` | IP/hostname of the Vantage controller |

### Optional (controller)
| Setting | Default | Description |
|---|---:|---|
| `VANTAGE_USER` | *(empty)* | Username (if required) |
| `VANTAGE_PASS` | *(empty)* | Password (if required) |

### Optional (MQTT)
| Setting | Default | Description |
|---|---:|---|
| `MQTT_HOST` | `127.0.0.1` | MQTT broker host |
| `MQTT_PORT` | `1883` | MQTT broker port |
| `MQTT_USERNAME` | *(empty)* | Broker username |
| `MQTT_PASSWORD` | *(empty)* | Broker password |
| `MQTT_TLS_ENABLED` | `false` | Enables TLS (basic toggle; see Notes) |
| `BASE_TOPIC` | `vantage` | Root topic for bridge topics |
| `DISCOVERY_PREFIX` | `homeassistant` | HA MQTT discovery prefix |

### Tuning knobs (Sniper behavior)
| Setting | Default | Description |
|---|---:|---|
| `POLL_INTERVAL` | `90` | Safety-net: force a full status check periodically |
| `POLL_QUIET_TIME` | `5` | Don’t poll if recent activity occurred (usually fade time + ~1s) |
| `COMMAND_THROTTLE_DELAY` | `0.02` | Delay between outgoing commands to protect serial buffers |

### Debug
| Setting | Default | Description |
|---|---:|---|
| `LOG_LEVEL` | `INFO` | Bridge log level |
| `PUBLISH_RAW_BUTTON_EVENTS` | `false` | Publishes raw JSON for every detected keypad event |

**Notes**
- TLS is currently a simple “on/off” toggle. If your broker requires CA/cert configuration, you may need to extend TLS settings in code.

---

## How keypad “Lazy Discovery” works (Log Tap)

The bridge attaches a custom `logging.Handler` to the `aiovantage` debug stream and parses `EL:` event log lines to detect:
- keypad button state changes
- task running state changes (virtual tasks)

On first sight of a button/task action, it publishes HA MQTT device trigger discovery and then publishes subsequent actions to MQTT.

---

## Dimming and ON/OFF semantics (important)

Home Assistant commonly sends:
- `.../set` with `ON` / `OFF`
- `.../brightness/set` with a value `0–255`

Bridge behavior:
- `brightness/set` with **0–255** sets the Vantage level proportionally.
- `brightness/set` with payload **`ON`** is treated as “restore last non-zero” level (per-load).
- `set` with **`ON`** restores last non-zero (default 100% if unknown).
- `set` with **`OFF`** turns off and publishes state immediately.

The bridge publishes:
- `.../state` as `ON` / `OFF`
- `.../brightness/state` as `0–255` for dimmable loads

---

## Cookbook: advanced automations

Because “Lazy Discovery” exposes keypad presses as triggers, you can build higher-level behaviors.

### Recipe 1: “Double-tap fixer” (resync a zombie light)

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

## MQTT topics

### Loads (lights)
- State: `BASE_TOPIC/light/<id>/state` → `ON` / `OFF`
- Command: `BASE_TOPIC/light/<id>/set` → `ON` / `OFF`
- Brightness state: `BASE_TOPIC/light/<id>/brightness/state` → `0–255`
- Brightness command: `BASE_TOPIC/light/<id>/brightness/set` → `0–255` (or `ON`)

### Keypads / tasks
- Action topic: `BASE_TOPIC/keypad/<station_or_task>/button/<pos>/action` → `press` / `release`
- Raw debug (optional): `BASE_TOPIC/keypad/_raw` → JSON

### Bridge status
- Availability: `BASE_TOPIC/bridge/status` → `online` / `offline`

### Diagnostics (published periodically)
- `BASE_TOPIC/diagnostics/cpu_usage_pct`
- `BASE_TOPIC/diagnostics/memory_usage_mb`
- `BASE_TOPIC/diagnostics/uptime_s`
- `BASE_TOPIC/diagnostics/messages_published_total`
- `BASE_TOPIC/diagnostics/entity_count`

---
---

## Known limitations and design choices (intentional)

This bridge intentionally favors **predictability** over “clever” optimizations, because small changes in MQTT timing can cause hard-to-debug Home Assistant behavior (duplicate commands, out-of-order state updates, and perceived “flapping” between ON/OFF and brightness).

### 1) Minimal command dedupe / debounce (by design)
Home Assistant may emit multiple MQTT commands for a single UI action (for example, `set=ON` and a brightness value close together).  
It is tempting to add aggressive dedupe/debounce, but in practice that can introduce race conditions where:
- one command is dropped incorrectly,
- brightness is applied without a preceding ON,
- HA and the bridge disagree on the final state.

Instead, the bridge relies on:
- controller-safe pacing (`COMMAND_THROTTLE_DELAY`)
- “restore last non-zero” semantics for ON
- state publication after each applied command

### 2) Keypad discovery depends on the controller event log
“Lazy discovery” is driven by parsing `EL:` lines from the controller’s event log stream via the `aiovantage` debug logger.  
If your controller/firmware does not emit those lines (or the stream is blocked), keypad triggers may not appear.

### 3) TLS support is basic
`MQTT_TLS_ENABLED=true` enables TLS with a minimal configuration.  
If your broker requires custom CA certificates or mutual TLS, you may need to extend the TLS parameters in code.

### 4) Entity model follows what works in HA
Loads are published as MQTT lights (with brightness topics). Some non-dimmable loads may still appear with brightness controls depending on the controller metadata. This is intentional to avoid changing entity types mid-stream and breaking dashboards/automations.


## Troubleshooting

### I don’t see entities in Home Assistant
- Confirm HA uses the same MQTT broker as the bridge
- Confirm MQTT discovery is enabled in HA
- Check broker traffic: `mosquitto_sub -t '#' -v` (careful: noisy)

### Keypads aren’t showing up
- Press each keypad button once after the bridge is running (lazy discovery)
- Confirm your controller produces event log (`EL:`) lines (see logs)

### Commands sometimes get ignored
- Increase `COMMAND_THROTTLE_DELAY` slightly (0.02 → 0.03–0.06)
- Avoid automations that toggle dozens of loads at once without delays

### State gets stale after running macros/scenes
- Increase `POLL_QUIET_TIME` to cover long fades/macros
- Verify fallback polling is enabled in the bridge (it is by default)

---

## Credits

- `loopj` for `aiovantage` and related Vantage tooling

