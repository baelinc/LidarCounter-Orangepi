#!/bin/bash

set -e

echo "Installing Lidar Car Counter for Orange Pi Zero 2..."

# -------------------------
# Path Updates for Orange Pi
# -------------------------
PROJECT_DIR="/root/LidarCounter-Orangepi"
# We'll use system python site-packages to avoid venv complexity on root
# but you can keep VENV if you prefer.
VENV_DIR="$PROJECT_DIR/venv"

# -------------------------
# Sanity checks
# -------------------------
if [ ! -d "$PROJECT_DIR" ]; then
  echo "ERROR: Project directory not found: $PROJECT_DIR"
  exit 1
fi

cd "$PROJECT_DIR"

# -------------------------
# System packages
# -------------------------
sudo apt update
sudo apt install -y \
  python3 \
  python3-pip \
  python3-venv \
  python3-serial \
  python3-requests \
  python3-flask \
  paho-mqtt-python \
  git \
  ntpsec-ntpdate

# -------------------------
# Enable UART5 (Orange Pi Specific)
# -------------------------
# On Orange Pi, we use /boot/orangepiEnv.txt instead of config.txt
ENV_CFG="/boot/orangepiEnv.txt"
UART_REBOOT_REQ=0

if [ -f "$ENV_CFG" ]; then
    if grep -q "uart5" "$ENV_CFG"; then
        echo "UART5 already enabled in $ENV_CFG"
    else
        echo "Enabling UART5 overlay..."
        # Append uart5 to overlays line or create it
        if grep -q "overlays=" "$ENV_CFG"; then
            sudo sed -i '/overlays=/ s/$/ uart5/' "$ENV_CFG"
        else
            echo "overlays=uart5" | sudo tee -a "$ENV_CFG"
        fi
        UART_REBOOT_REQ=1
    fi
else
    echo "WARNING: $ENV_CFG not found. Make sure UART5 is enabled manually."
fi

# -------------------------
# Permissions (Running as Root)
# -------------------------
# Since you are running as root, we don't need the sudoers lidar file.
# However, we ensure the service file points to the right user.
SUDOERS_FILE="/etc/sudoers.d/lidar"
if [ -f "$SUDOERS_FILE" ]; then
    sudo rm "$SUDOERS_FILE"
fi

# -------------------------
# Python Environment
# -------------------------
if [ ! -d "$VENV_DIR" ]; then
  echo "Creating virtual environment at $VENV_DIR"
  python3 -m venv "$VENV_DIR"
fi

source "$VENV_DIR/bin/activate"
pip install --upgrade pip
if [ -f "requirements.txt" ]; then
    pip install -r requirements.txt
fi

# -------------------------
# systemd service
# -------------------------
SERVICE_FILE="systemd/LidarCounter.service"
if [ -f "$SERVICE_FILE" ]; then
  echo "Installing systemd service"
  # Update the service file paths to /root/ before copying
  sed -i "s|/home/admin/LidarCounter|$PROJECT_DIR|g" "$SERVICE_FILE"
  sed -i "s|User=admin|User=root|g" "$SERVICE_FILE"
  
  sudo cp "$SERVICE_FILE" /etc/systemd/system/LidarCounter.service
  sudo systemctl daemon-reload
  sudo systemctl enable LidarCounter
  sudo systemctl restart LidarCounter
else
  echo "WARNING: systemd/LidarCounter.service not found"
fi

# -------------------------
# Final notes
# -------------------------
echo ""
echo "Install complete."
echo "Web UI: http://localhost:80" 
echo ""

if [ "$UART_REBOOT_REQ" = "1" ]; then
  echo "*****************************************************"
  echo "UART5 OVERLAY WAS ADDED."
  echo "A REBOOT IS REQUIRED to activate Pins 8 and 10."
  echo "*****************************************************"
  read -p "Reboot now? (y/N): " REBOOT
  if [[ "$REBOOT" =~ ^[Yy]$ ]]; then
    sudo reboot
  fi
fi
