#!/bin/bash

PROJECT_DIR="/home/admin/ShowMonLidarCounter"
BACKUP_PATH="/tmp/config.json.bak"

echo "--- Starting System Update ---"

# 1. Enter directory
cd $PROJECT_DIR || { echo "Directory not found"; exit 1; }

# 2. Backup config.json (The Lifeboat)
if [ -f "config.json" ]; then
    echo "Backing up config.json..."
    cp config.json $BACKUP_PATH
fi

# 3. Pull from GitHub
echo "Fetching latest code from GitHub..."
# This uses the URL already configured in your git remote
git fetch --all
git reset --hard origin/main

# 4. Restore config.json
if [ -f $BACKUP_PATH ]; then
    echo "Restoring config.json..."
    cp $BACKUP_PATH config.json
fi

# 5. Restart Services
echo "Restarting services..."
sudo systemctl restart ShowMonLidarCounter.service
sudo systemctl restart LidarCounter.service

echo "--- Update Complete! ---"
sudo systemctl status ShowMonLidarCounter.service --no-pager
