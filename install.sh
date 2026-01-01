#!/bin/bash

#Lidar Counter
#Written by Kevin Rhodus // Forked and changes made by Johnathan Evans
#christmas@onthehill.us
#Version 1.0.0

set -e

echo "Installing Lidar Car Counter..."

PROJECT_DIR="/home/admin/LidarCounter"
VENV_DIR="/home/admin/LidarCounter/venv"
UART_CFG="/boot/firmware/config.txt"

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
  python3-serial

# -------------------------
# Serial / UART dependencies
# -------------------------
sudo apt install -y python3-serial

# -------------------------
# Enable UART (TFmini requires hardware serial)
# -------------------------
UART_CFG="/boot/firmware/config.txt"
UART_ENABLED=0

if [ ! -f "$UART_CFG" ]; then
  echo "ERROR: $UART_CFG not found. Unexpected OS layout."
  exit 1
fi

if grep -q "^enable_uart=1" "$UART_CFG"; then
  echo "UART already enabled."
else
  echo "Enabling UART in $UART_CFG"
  echo "" | sudo tee -a "$UART_CFG" >/dev/null
  echo "enable_uart=1" | sudo tee -a "$UART_CFG" >/dev/null
  UART_ENABLED=1
fi

# -------------------------
# Disable serial console, keep UART hardware
# -------------------------
if command -v raspi-config >/dev/null 2>&1; then
  echo "Configuring serial interface (disable login shell, enable UART)"
  sudo raspi-config nonint do_serial_cons 1
  sudo raspi-config nonint do_serial_hw 0
else
  echo "WARNING: raspi-config not found, skipping serial console config"
fi

# -------------------------
# Allow app to restart its own systemd service
# -------------------------
SUDOERS_FILE="/etc/sudoers.d/lidar"

if [ ! -f "$SUDOERS_FILE" ]; then
  echo "Configuring sudo permissions for service restart"

  sudo tee "$SUDOERS_FILE" >/dev/null <<EOF
admin ALL=NOPASSWD: /bin/systemctl restart LidarCounter
EOF

  sudo chmod 440 "$SUDOERS_FILE"
else
  echo "Sudoers rule for LidarCounter already exists"
fi
# -------------------------
# Python virtual environment
# -------------------------
if [ ! -d "$VENV_DIR" ]; then
  echo "Creating virtual environment at $VENV_DIR"
  python3 -m venv "$VENV_DIR"
else
  echo "Using existing virtual environment at $VENV_DIR"
fi

source "$VENV_DIR/bin/activate"

pip install --upgrade pip
pip install -r requirements.txt

# -------------------------
# systemd service
# -------------------------
if [ -f "systemd/LidarCounter.service" ]; then
  echo "Installing systemd service"
  sudo cp systemd/LidarCounter.service /etc/systemd/system/LidarCounter.service
  sudo systemctl daemon-reload
  sudo systemctl enable LidarCounter
  sudo systemctl restart LidarCounter
else
  echo "WARNING: systemd/LidarCounter.service not found — skipping service install"
fi

# -------------------------
# Final notes
# -------------------------
echo ""
echo "Install complete."
echo "Web UI: http://<pi-ip>/:8080"
echo ""

if [ "$UART_ENABLED" = "1" ]; then
  echo "*****************************************************"
  echo "UART WAS JUST ENABLED."
  echo "A REBOOT IS REQUIRED before the sensor will work."
  echo "*****************************************************"
  echo ""
  read -p "Reboot now? (y/N): " REBOOT
  if [[ "$REBOOT" =~ ^[Yy]$ ]]; then
    sudo reboot
  else
    echo "Please reboot manually before using the system."
  fi
fi
