# Vantage → MQTT Bridge for Home Assistant

A robust, opinionated bridge that connects your **Vantage InFusion** lighting system to **MQTT**, for seamless use with **Home Assistant** (or anything else speaking MQTT).

This project began as a solution to *real-world reliability problems*: after struggling with controller lockups and sometimes having to reboot my hardware every few days using other approaches, I created a bridge that runs fully outside of Home Assistant, talking plain MQTT for stability and simplicity.

---

## Why choose this bridge?

There is an excellent (and recommended!) third-party Home Assistant integration for Vantage: [loopj/home-assistant-vantage](https://github.com/loopj/home-assistant-vantage).  
If you want built-in HA entities, config flow, and Home Assistant UI integration, please try that first.

This bridge is for those who:

- Need **maximum reliability**: It decouples Vantage from Home Assistant, meaning crashes, reloads, or upgrades in HA won’t affect your lighting system.
- Have experienced **controller crashes or flakiness** with direct integrations; after many controller reboots, I wanted a solution that is robust and easy to debug.
- Prefer **MQTT-based automation, scripting, or infrastructure**.
- Want to use Vantage loads and data in other systems (Node-RED, custom services, etc.), not just inside HA.
- Like the transparency of seeing every MQTT topic and decoupled “plumbing”.

Loopj’s integration is highly recommended if direct, native Home Assistant integration is your goal. My project is a complement—not a competitor—and is offered to the community having solved my own stability headaches!

---

## Features

- Talks to your Vantage InFusion controller using `aiovantage`.
- Exposes each **Load** (and select groups) as MQTT “light” topics.
- Adds rich metadata (`area`, `name`, `id`) as JSON attributes.
- Stays fully outside of Home Assistant—just use MQTT!
- Handles both state and brightness commands, as well as reporting bridge health.

### MQTT Topics

- State: `vantage/light/<id>/state` (`ON`/`OFF`)
- Brightness: `vantage/light/<id>/brightness/state` (`0–255`)
- Attributes: `vantage/light/<id>/attributes` (JSON, e.g. `area`, `name`, `id`)
- Commands:  
  - `vantage/light/<id>/set` (`"ON"`/`"OFF"`)  
  - `vantage/light/<id>/brightness/set` (`0–255`)
- Bridge health: `vantage/bridge/status` (`"online"`/`"offline"`)

> **Default prefix:** `vantage` (configurable in YAML).

---

## Installation

### 1. Clone the repo

```sh
git clone https://github.com/srhunt-cyber/Vantage---MQTT-bridge-for-Home-Assistant.git
cd Vantage---MQTT-bridge-for-Home-Assistant
```

### 2. Create a virtualenv & install requirements

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r bridge/requirements.txt
```
Dependencies:  
- `aiovantage` – async Vantage client  
- `paho-mqtt` – MQTT client  
- `PyYAML` – config parsing

### 3. Configure your bridge

Copy the example config somewhere convenient (e.g. `~/.config/`):

```sh
mkdir -p ~/.config
cp config/vantage_mqtt_bridge.yaml.example ~/.config/vantage_mqtt_bridge.yaml
```

Edit `~/.config/vantage_mqtt_bridge.yaml`:

```yaml
vantage:
  host: 192.168.1.39      # IP / hostname of your Vantage controller
  port: 3001              # Typical Infusion TCP port
  username: ""            # If your system uses auth
  password: ""

mqtt:
  host: rtipoll.local     # Your MQTT broker host
  port: 1883
  username: ""            # If needed
  password: ""
  client_id: vantage_mqtt_bridge

bridge:
  topic_prefix: "vantage"
  retain: false
  log_level: "INFO"
```

### 4. Run the bridge

```sh
python bridge/vantage_mqtt_bridge.py --config ~/.config/vantage_mqtt_bridge.yaml
```

You should see logs:

```
INFO - Connecting to Vantage at 192.168.1.39 ...
INFO - Vantage connected.
INFO - MQTT connected to rtipoll.local:1883
INFO - Discovering loads...
```

---

## Example: Home Assistant MQTT Light

A minimal MQTT light config for a single load (see `examples/home_assistant_mqtt.yaml`):

```yaml
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
```

Use HA’s Helpers/Groups or further YAML to group by room or area.

---

## Systemd Service (optional)

Want to run on boot? Use the included systemd service example (`config/vantage_mqtt_bridge.service.example`):

```ini
[Unit]
Description=Vantage MQTT Bridge
After=network-online.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/home/YOUR_USER/path-to-repo
ExecStart=/home/YOUR_USER/.venv/bin/python bridge/vantage_mqtt_bridge.py --config /home/YOUR_USER/.config/vantage_mqtt_bridge.yaml
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Install with:

```sh
sudo cp config/vantage_mqtt_bridge.service.example /etc/systemd/system/vantage_mqtt_bridge.service
sudo systemctl daemon-reload
sudo systemctl enable --now vantage_mqtt_bridge.service
```

---

## MQTT Topic Reference

- **Bridge status:** `vantage/bridge/status` (`"online"` / `"offline"`)
- **Load attributes:** `vantage/light/<id>/attributes`
  - Example payload:
    ```json
    {
      "vantage_area": "Kitchen",
      "vantage_id": 278,
      "vantage_name": "Perimeter"
    }
    ```
- **State:** `vantage/light/<id>/state` (`"ON"` / `"OFF"`)
- **Brightness:** `vantage/light/<id>/brightness/state` (`0–255`)
- **Commands:**
  - `vantage/light/<id>/set` (`"ON"`/`"OFF"`)
  - `vantage/light/<id>/brightness/set` (`0–255`)

---

## Repository Layout

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
```

---

## Roadmap & Contributing

This project works well for "lights via MQTT" already, but there’s room for growth!
- Home Assistant MQTT discovery (auto-publish config topics)
- Mapping Vantage labels to device types (treat “Relay/Motor” loads as switches, etc.)
- Additional Vantage objects:  
  - Keypad events as MQTT topics  
  - Tasks & variables  
  - Blinds/covers as MQTT covers  
- Debug tools & protocol passthrough

**Pull requests are very welcome!** Please open an issue if you have questions or would like to share your improvements.

---

## Credits

- Built with [aiovantage](https://github.com/joeldebruijn/aiovantage).
- Inspired by [loopj/home-assistant-vantage](https://github.com/loopj/home-assistant-vantage) and early work on RTI-AD8x-Home-Assistant-bridge.
- MIT licensed. Enjoy!

