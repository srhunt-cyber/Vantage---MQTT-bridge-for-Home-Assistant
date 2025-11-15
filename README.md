# Vantage → MQTT bridge for Home Assistant

A small, opinionated bridge that connects a **Vantage InFusion** lighting system to **MQTT**, so you can use it with **Home Assistant** (or anything else that speaks MQTT).

This project is intentionally simple:

- Uses `aiovantage` to talk to your Vantage controller.
- Publishes every Vantage load as MQTT topics.
- Adds useful metadata (`area`, `name`, `id`) as attributes.
- Stays out of Home Assistant’s core – everything is plain MQTT.

If you want a fully native HA integration with config-flow, buttons, tasks, etc., you probably want the excellent `home-assistant-vantage` integration. This project is for people who prefer **MQTT + YAML + full control**, or who already have a heavy MQTT/automation setup.

---

## Features

### What it does today

- Connects to a Vantage InFusion controller using `aiovantage`.
- Exposes each **Load** (and certain load groups) as MQTT “lights”:

  - State topic:
    - `vantage/light/<id>/state` → `ON` / `OFF`
  - Brightness topic:
    - `vantage/light/<id>/brightness/state` → `0–255`
  - Attributes topic:
    - `vantage/light/<id>/attributes` → JSON with:
      - `vantage_area` – Vantage “area” (e.g. `"Kitchen"`, `"Master Suite"`)
      - `vantage_name` – Load name (e.g. `"Perimeter"`, `"Ceiling"`)
      - `vantage_id` – Vantage numeric ID

- Accepts commands from MQTT:

  - `vantage/light/<id>/set` → `"ON"` / `"OFF"`
  - `vantage/light/<id>/brightness/set` → `0–255` brightness

- Publishes bridge status:
  - `vantage/bridge/status` → `"online"` / `"offline"`

> **Note:** The default topic prefix is `vantage`. You can change that in the YAML config.

---

## Why would I use this instead of the official HA Vantage integration?

You might prefer this bridge if:

- You’re already **MQTT-centric** and want Vantage to “look like” the rest of your MQTT devices.
- You like having **complete control over entity names, groups, and logic** in Home Assistant YAML.
- You want to keep Vantage ↔ HA **loosely coupled** via MQTT.
- You want to reuse the Vantage data in other tools (Node-RED, custom services, etc.)

If you want full click-through HA UX, auto-discovery, blueprints, card helpers, etc., the native integration is a better fit. This bridge is more “plumbing” than “product.”

---

## Repository layout

```text
.
├── bridge/
│   ├── vantage_mqtt_bridge.py      # Main bridge script
│   └── requirements.txt            # Python dependencies
├── config/
│   ├── vantage_mqtt_bridge.yaml.example     # Example bridge config
│   └── vantage_mqtt_bridge.service.example  # Example systemd unit
├── examples/
│   └── home_assistant_mqtt.yaml   # Example HA MQTT light config
├── LICENSE
└── README.md

Installation
1. Clone the repo
cd ~/code
git clone git@github.com:srhunt-cyber/Vantage---MQTT-bridge-for-Home-Assistant.git
cd Vantage---MQTT-bridge-for-Home-Assistant
2. Create a virtualenv and install requirements
python3 -m venv .venv
source .venv/bin/activate

pip install -r bridge/requirements.txt
Dependencies (from bridge/requirements.txt):
aiovantage – async client for Vantage InFusion
paho-mqtt – MQTT client
PyYAML – config parsing
Configuration
Copy the example config somewhere convenient (e.g. ~/.config):
mkdir -p ~/.config
cp config/vantage_mqtt_bridge.yaml.example ~/.config/vantage_mqtt_bridge.yaml
Edit ~/.config/vantage_mqtt_bridge.yaml and set your values:
vantage:
  host: 192.168.1.39      # IP / hostname of your Vantage controller
  port: 3001              # Typical Infusion TCP port
  username: ""            # If your system uses auth
  password: ""

mqtt:
  host: rtipoll.local     # Your MQTT broker host
  port: 1883
  username: ""            # If your broker requires auth
  password: ""
  client_id: vantage_mqtt_bridge

bridge:
  topic_prefix: "vantage"                 # All topics are under this prefix
  retain: false                           # Whether to retain state messages
  log_level: "INFO"                       # DEBUG / INFO / WARNING / ERROR
  # (Additional options may be added over time)
Running the bridge
From the repo (with your venv active):
python bridge/vantage_mqtt_bridge.py \
  --config ~/.config/vantage_mqtt_bridge.yaml
You should see logs like:
INFO - Connecting to Vantage at 192.168.1.39 ...
INFO - Vantage connected.
INFO - MQTT connected to rtipoll.local:1883
INFO - Discovering loads...
In another shell you can watch MQTT traffic:
mosquitto_sub -h rtipoll.local -p 1883 -v -R \
  -t 'vantage/#'
You’ll see lines like:
vantage/bridge/status online
vantage/light/278/attributes {"vantage_area": "Kitchen", "vantage_id": 278, "vantage_name": "Perimeter"}
vantage/light/278/state ON
vantage/light/278/brightness/state 255
Example: Home Assistant MQTT light
The examples/home_assistant_mqtt.yaml file contains a working example. Here’s a simplified version for a single load:
light:
  - platform: mqtt
    name: "Kitchen Perimeter"
    unique_id: vantage_light_278
    state_topic: "vantage/light/278/state"
    command_topic: "vantage/light/278/set"

    brightness_state_topic: "vantage/light/278/brightness/state"
    brightness_command_topic: "vantage/light/278/brightness/set"
    brightness_scale: 255

    payload_on: "ON"
    payload_off: "OFF"
    qos: 1
    retain: false
You can create a little helper template to build groups, or use the Home Assistant UI → Helpers → Light group feature to group them by room (e.g. “Kitchen All Lights”, “Master Suite All Lights”, etc.).
Systemd service (optional)
If you want the bridge to start on boot, there is an example systemd unit at:
config/vantage_mqtt_bridge.service.example
Rough structure (simplified):
[Unit]
Description=Vantage MQTT Bridge
After=network-online.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/home/YOUR_USER/code/Vantage---MQTT-bridge-for-Home-Assistant
ExecStart=/home/YOUR_USER/.venv/bin/python \
  bridge/vantage_mqtt_bridge.py \
  --config /home/YOUR_USER/.config/vantage_mqtt_bridge.yaml
Restart=on-failure

[Install]
WantedBy=multi-user.target
Copy it to:
sudo cp config/vantage_mqtt_bridge.service.example \
  /etc/systemd/system/vantage_mqtt_bridge.service

sudo systemctl daemon-reload
sudo systemctl enable --now vantage_mqtt_bridge.service
MQTT topic reference
By default (topic_prefix: "vantage"):
Bridge status
vantage/bridge/status → "online" / "offline"
Per-load topics (<id> is the Vantage load ID)
Attributes:
vantage/light/<id>/attributes
Example payload:
{
  "vantage_area": "Kitchen",
  "vantage_id": 278,
  "vantage_name": "Perimeter"
}
State:
vantage/light/<id>/state → "ON" / "OFF"
Brightness state:
vantage/light/<id>/brightness/state → 0–255
Command topics:
vantage/light/<id>/set → "ON" / "OFF"
vantage/light/<id>/brightness/set → 0–255
Roadmap / ideas for contributors
This bridge works well today for “lights via MQTT”, but there’s a lot of room for growth. Ideas (most are straightforward, just need time + hardware to test):
Home Assistant MQTT discovery
Auto-publish homeassistant/light/…/config topics based on Vantage area/name.
Map Vantage labels → device classes
E.g. treat “Relay/Motor” loads as switches instead of lights.
Expose more Vantage objects
Keypad events as MQTT topics.
Tasks / variables as MQTT entities (numbers, text, binary sensors).
Blind/cover objects → MQTT covers.
Debug / passthrough tools
Optional MQTT “debug” topic to inject raw commands or log Vantage protocol traffic.
If you add something cool, PRs are very welcome.
Credits
Built on top of the awesome aiovantage library.
Inspired by the official home-assistant-vantage integration and by the earlier RTI-AD8x-Home-Assistant-bridge.
MIT licensed. Enjoy!
