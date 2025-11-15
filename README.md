# Home Assistant Vantage MQTT Bridge

Bridge between a Vantage Infusion/Equinox lighting system and MQTT,
so that Home Assistant can auto-discover and control Vantage loads
via the MQTT Light platform.

## Features

- Connects directly to Vantage controller via TCP (not via MQTT)
- Publishes light states and brightness to MQTT topics
- Subscribes to MQTT set topics to control Vantage loads
- Publishes metadata (area, name, id) as attributes
- Designed to work cleanly with Home Assistant's MQTT discovery

## Project Status

This is a work-in-progress personal project migrated from a
working Home Assistant installation. Use at your own risk :)

Tested on:

- Home Assistant OS (2025.x)
- MQTT broker: Mosquitto
- Vantage controller: Infusion

## Quick Start

1. Create a virtual environment:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
