# Vantage (InFusion) MQTT Bridge for Home Assistant

This is a standalone Python bridge that connects a Legrand Vantage InFusion lighting controller to Home Assistant via MQTT Auto-Discovery.

This project began as a solution to *real-world reliability problems*. After struggling with controller lockups and instability using other approaches, I created this bridge to run as a separate, persistent service. It communicates with Home Assistant via plain MQTT for maximum stability and simplicity.

---

## Why Choose This Bridge?

This project is an alternative—not a competitor—to the excellent [in-HA Vantage Integration](https://github.com/loopj/home-assistant-vantage). It is offered to the community for those who prefer a decoupled, service-based architecture.

This bridge is for those who:

* Need **maximum reliability**: It decouples Vantage from Home Assistant. Reboots, reloads, or upgrades in HA won’t affect your lighting bridge.
* Have experienced **controller flakiness** with direct integrations.
* Prefer **MQTT-based infrastructure** for automation and debugging.
* Want to use Vantage data in other systems (Node-RED, etc.), not just Home Assistant.

---

## Features

* **Standalone Service:** Runs as a separate `systemd` service for high reliability.
* **MQTT Auto-Discovery:** All Vantage loads are automatically discovered by Home Assistant. No manual YAML configuration is required.
* **Full Dimming Support:** Correctly handles dimming, On/Off buttons, and "restore last state" from the Home Assistant UI.
* **Automatic Area Assignment:** Automatically detects the Area/Room for each light from your Vantage config and assigns it in Home Assistant using the `suggested_area` feature. This makes it much easier to identify and organize your lights.
* **Health & Monitoring:** Publishes its own health (uptime, CPU, memory) and connection status to MQTT for monitoring in HA.

## Prerequisites

This is **not** a plug-and-play Home Assistant Add-on. It is intended for users who are comfortable with:
1.  Running Python scripts on a Linux host (like the Home Assistant VM, or another server).
2.  Managing a `systemd` service (or `screen`, `tmux`, etc.) to keep the script running.
3.  Setting up an MQTT broker.

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
    Copy `.env.example` to `.env` and edit it with your Vantage and MQTT broker IP addresses and credentials.

    ```bash
    cp .env.example .env
    nano .env
    ```

4.  **Run the Bridge (Test):**
    You can run it directly from your terminal to test.
    ```bash
    python vantage_bridge-monitoring.py
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

## ✨ Entity Naming and Home Assistant Area Support

### Migrating from the In-HA Vantage Integration?

This bridge was designed to be a drop-in replacement. It uses the same `slugify` naming logic as the popular **Vantage InFusion (Asyncio) integration** by `loopj`.

This means your existing `entity_id`s (e.g., `light.fixture_5`, `light.fan_2_load`) should be **identical**. You can migrate to this bridge with a high chance of preserving all your existing dashboards and automations.

**Migration Steps:**
1.  **As always, back up your Home Assistant configuration first.**
2.  Stop or remove the old integration.
3.  Run this bridge script (v0.9.13 or later).
4.  In Home Assistant, go to **Developer Tools > YAML** and click **"Reload MQTT Entities"**.
5.  Your existing `entity_id`s should now be populated by this bridge.

### New in v0.9.13: Automatic Area Assignment

This bridge creates a unique "device" in Home Assistant for *each individual light*. This allows the script to add the `suggested_area` property to each light, so HA will automatically know which room (e.g., "Kitchen," "Living Room") each light belongs to.

This finally solves the problem of identifying which `light.fixture_5` belongs to which room.

### Upgrading from an older bridge version (pre-v0.9.13)?

This is a **non-breaking update** and is safe to apply. Your existing dashboards and automations will **not** break.

The bridge uses the same `object_id` logic as before. Your existing entities (e.g., `light.fixture_5`) will just be re-assigned to their new, individual devices without changing their `entity_id`.

**Upgrade Steps:**
1.  Stop your old bridge script.
2.  Pull the new code and start the new bridge script.
3.  In Home Assistant, go to **Developer Tools > YAML** and click **"Reload MQTT Entities"**.
4.  (Optional Cleanup) Go to **Settings > Devices & Services > Integrations > MQTT**. You can find and **delete** the old, single "Vantage Controller" device, which will now show 0 entities.

---

## Monitoring Dashboard

This repository includes YAML for a Home Assistant dashboard to monitor the bridge's health, as well as the `mqtt/mqtt_sensors.yaml` configuration to create the sensors.

1.  Add the contents of `mqtt_sensors.yaml` to your Home Assistant MQTT configuration.
2.  Install the `custom:gauge-card` from HACS (in the HACS "Frontend" store).
3.  Create a new dashboard and use the YAML from `lovelace_dashboard.yaml`.

---

## MQTT Topic Reference

This bridge uses MQTT Auto-Discovery, so you do not need to configure these topics manually in Home Assistant. This is for debugging or non-HA use.

* **Bridge Status:** `vantage/bridge/status` (`"online"` / `"offline"`)
* **Load State:** `vantage/light/<id>/state` (Publishes `"ON"` / `"OFF"`)
* **Load Brightness:** `vantage/light/<id>/brightness/state` (Publishes `0-255`)
* **Load Attributes:** `vantage/light/<id>/attributes`
    ```json
    {
      "vantage_area": "Kitchen",
      "vantage_id": 278,
      "vantage_name": "Perimeter"
    }
    ```
* **On/Off Commands:** `vantage/light/<id>/set` (Expects `"ON"` / `"OFF"`)
* **Brightness Commands:** `vantage/light/<id>/brightness/set` (Expects `0-255`)
* **Bridge Diagnostics:** `vantage/diagnostics/<metric>` (e.g., `vantage/diagnostics/cpu_usage_pct`)

---

## Roadmap & Contributing

This project works well for "lights via MQTT," but there’s room for growth!
* Mapping Vantage labels to device types (treat “Relay/Motor” loads as switches, etc.)
* Additional Vantage objects:
    * Keypad events as MQTT topics
    * Tasks & variables
    * Blinds/covers as MQTT covers

**Pull requests are very welcome!** Please open an issue if you have questions or would like to share your improvements.

---

## Credits

* This bridge is powered by the [**`aiovantage`** library](https://github.com/loopj/aiovantage).
* MIT Licensed.
