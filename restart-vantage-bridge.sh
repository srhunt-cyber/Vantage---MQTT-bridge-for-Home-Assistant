#!/bin/bash

# --- Configuration ---
# Set this to the name of your systemd unit file
SERVICE_NAME="vantage-bridge.service" 

# Set this to the absolute path of your project directory
# IMPORTANT: This must match the WorkingDirectory in your systemd file
PROJECT_DIR="/home/srhunt64/services/vantage_bridge" 
# ---------------------

echo "=========================================="
echo "🚀 Bouncing Vantage MQTT Bridge Service"
echo "=========================================="

# 1. Stop the running service
echo "Stopping the ${SERVICE_NAME} service..."
sudo systemctl stop "$SERVICE_NAME"
if [ $? -ne 0 ]; then
    echo "ERROR: Failed to stop service. Exiting."
    exit 1
fi
echo "Service stopped successfully."
echo "------------------------------------------"

# 2. Update Code and Dependencies
#echo "Changing to project directory: ${PROJECT_DIR}"
#cd "$PROJECT_DIR" || exit

#echo "Pulling latest changes from Git..."
#git pull

#echo "Updating Python dependencies..."
# Assumes your virtual environment is activated by the systemd service file, 
# or you use the absolute path to the pip installation as defined in your service file.
# We'll use the venv path for reliability.
#VENV_PATH="${PROJECT_DIR}/.venv"
#PIP_EXECUTE="${VENV_PATH}/bin/pip"

# If you created your venv using python3 -m venv .venv, this should work:
#if [ -f "$PIP_EXECUTE" ]; then
#    "$PIP_EXECUTE" install -r requirements.txt
#else
#    echo "WARNING: Could not find pip executable at ${PIP_EXECUTE}. Skipping dependency update."
#fi
echo "------------------------------------------"


# 3. Start the service
echo "Starting the ${SERVICE_NAME} service..."
sudo systemctl start "$SERVICE_NAME"

if [ $? -eq 0 ]; then
    echo "SUCCESS: Service started."
    echo "Use 'sudo journalctl -fu ${SERVICE_NAME}' to view logs."
else
    echo "ERROR: Failed to start service. Check logs immediately."
fi

echo "=========================================="

