#!/bin/bash

# --- Updated paths for Orange Pi Zero 2 ---
PROJECT_DIR="/root/LidarCounter-Orangepi"
CONFIG_BACKUP="/tmp/config.json.bak"
SCHED_BACKUP="/tmp/schedule.json.bak"

echo "=========================================="
echo "   Starting Lidar Counter System Update   "
echo "=========================================="

# 1. Enter directory
if cd "$PROJECT_DIR"; then
    echo "Current directory: $PROJECT_DIR"
else
    echo "ERROR: Directory $PROJECT_DIR not found!"
    exit 1
fi

# 2. Backup Local Data (The Lifeboat)
# We backup config and schedule so local changes aren't lost
if [ -f "config.json" ]; then
    echo "-> Backing up config.json..."
    cp config.json "$CONFIG_BACKUP"
fi

if [ -f "schedule.json" ]; then
    echo "-> Backing up schedule.json..."
    cp schedule.json "$SCHED_BACKUP"
fi

# 3. Pull from GitHub
echo "-> Fetching latest code from GitHub..."
# This force-aligns your local files with the remote repository
git fetch --all
git reset --hard origin/main

# 4. Restore Local Data
if [ -f "$CONFIG_BACKUP" ]; then
    echo "-> Restoring config.json..."
    cp "$CONFIG_BACKUP" config.json
fi

if [ -f "$SCHED_BACKUP" ]; then
    echo "-> Restoring schedule.json..."
    cp "$SCHED_BACKUP" schedule.json
fi

# 5. Update Python Dependencies
# Just in case requirements.txt was changed in the update
echo "-> Checking for new Python dependencies..."
./venv/bin/pip install -r requirements.txt --quiet

# 6. Restart Service
echo "-> Restarting LidarCounter service..."
systemctl restart LidarCounter.service

echo "=========================================="
echo "            Update Complete!              "
echo "=========================================="

# Show status to confirm it's back up
systemctl status LidarCounter.service --no-pager
