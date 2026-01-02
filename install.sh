#!/bin/bash

# --- 1. Define the correct Orange Pi path ---
PROJECT_DIR="/root/LidarCounter-Orangepi"

echo "=== Starting Orange Pi Lidar Installation ==="

# --- 2. Create the directory if it doesn't exist ---
if [ ! -d "$PROJECT_DIR" ]; then
    echo "Creating project directory..."
    mkdir -p "$PROJECT_DIR"
fi

cd "$PROJECT_DIR" || { echo "Failed to enter directory"; exit 1; }

# --- 3. Hardware Setup: Enable UART5 ---
echo "Enabling UART5 hardware overlay..."
if ! grep -q "overlays=uart5" /boot/orangepiEnv.txt; then
    echo "overlays=uart5" >> /boot/orangepiEnv.txt
    echo "Hardware overlay added. A reboot will be required later."
fi

# --- 4. Install System Packages (APT) ---
echo "Installing system dependencies..."
apt-get update
apt-get install -y python3-venv python3-pip python3-dev build-essential git

# --- 5. Setup Python Virtual Environment ---
echo "Setting up Virtual Environment..."
# This avoids the 'Unable to locate package' and 'Managed Environment' errors
python3 -m venv venv

# --- 6. Install Python Libraries (PIP) ---
echo "Installing Python requirements..."
# Ensure requirements.txt uses 'paho-mqtt', NOT 'python3-paho-mqtt'
./venv/bin/pip install --upgrade pip
./venv/bin/pip install flask flask-cors pyserial paho-mqtt requests

# --- 7. Install Systemd Service ---
echo "Installing LidarCounter Service..."

# Check the systemd subfolder instead of the root folder
if [ -f "systemd/LidarCounter.service" ]; then
    cp systemd/LidarCounter.service /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable LidarCounter.service
    systemctl restart LidarCounter.service
    echo "Service installed and started from systemd folder."
else
    echo "ERROR: Service file NOT found at $PROJECT_DIR/systemd/LidarCounter.service"
    exit 1
fi

echo "=== Installation Finished ==="
echo "NOTE: If this is the first time enabling UART5, please type 'reboot' now."
