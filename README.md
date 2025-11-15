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
