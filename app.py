#!/usr/bin/env python3
import os
import sys
import time
import json
import sqlite3
import threading
import logging
import subprocess
import csv
import io
from datetime import datetime, timezone

import serial
import requests
import paho.mqtt.client as mqtt
from flask import Flask, render_template, jsonify, request, send_file, make_response

# --- PATH CONFIGURATION ---
BASE_DIR = "/root/LidarCounter-Orangepi"
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')
SCHEDULE_FILE = os.path.join(BASE_DIR, 'schedule.json')

# Logging Setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# --- CONFIG LOADER ---
def load_config():
    if not os.path.exists(CONFIG_FILE):
        logger.error(f"Config file missing at {CONFIG_FILE}")
        sys.exit(1)
    with open(CONFIG_FILE, 'r') as f:
        return json.load(f)

cfg = load_config()

# Extract Vars from Config
SERIAL_PORT = cfg.get('serial_port', '/dev/ttyS5')
BAUD_RATE = cfg.get('baudrate', 115200)
DB_PATH = os.path.join(BASE_DIR, cfg.get('database', 'cars.db'))

# Detection Settings
DETECTION_CFG = cfg.get('detection', {})
DEBOUNCE_MS = DETECTION_CFG.get('debounce_ms', 200)
MIN_STRENGTH = DETECTION_CFG.get('min_strength', 100)

# MQTT Config
MQTT_CFG = cfg.get('mqtt', {})

# System Update Config
UPDATE_CFG = cfg.get('system_update', {
    "repo_url": "https://github.com/baelinc/LidarCounter-Orangepi.git",
    "branch": "main",
    "local_path": BASE_DIR,
    "service_name": "LidarCounter.service"
})

# --- DATABASE ENGINE ---
def get_db_connection():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS detections 
                      (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                       timestamp TEXT NOT NULL, 
                       distance INTEGER, 
                       strength INTEGER)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS metadata 
                      (key TEXT PRIMARY KEY, value TEXT)''')
    cursor.execute("INSERT OR IGNORE INTO metadata (key, value) VALUES ('total_count', '0')")
    conn.commit()
    conn.close()

# --- STATE ---
state = {
    "current_distance": 0,
    "current_strength": 0,
    "car_present": False,
    "is_active_by_schedule": False,
    "manual_override": False,
    "test_mode": DETECTION_CFG.get('test_mode', False),
    "schedule_status": "Initializing"
}
state_lock = threading.Lock()

# --- SCHEDULING SYSTEM ---
def check_schedule():
    while True:
        try:
            if os.path.exists(SCHEDULE_FILE):
                with open(SCHEDULE_FILE, 'r') as f:
                    schedule = json.load(f)
                
                now = datetime.now()
                # 0=Mon, 6=Sun in Python; Adjusting to your JSON (0=Sun)
                weekday_index = (now.weekday() + 1) % 7
                today_sched = schedule[weekday_index]

                if not today_sched.get('Enable', False):
                    new_status = False
                else:
                    start_t = datetime.strptime(today_sched['StartShow'], "%H:%M").time()
                    stop_t = datetime.strptime(today_sched['ShowStop'], "%H:%M").time()
                    now_t = now.time()

                    if start_t <= stop_t:
                        new_status = start_t <= now_t <= stop_t
                    else: # Crosses Midnight
                        new_status = now_t >= start_t or now_t <= stop_t

                with state_lock:
                    state["is_active_by_schedule"] = new_status
                    state["schedule_status"] = "Active" if new_status else "Outside Window"
            else:
                with state_lock: state["schedule_status"] = "Schedule File Missing"
        except Exception as e:
            logger.error(f"Schedule Error: {e}")
        time.sleep(30)

# --- LIDAR SENSOR ENGINE ---
def lidar_engine():
    try:
        ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=0.1)
        logger.info(f"Lidar started on {SERIAL_PORT}")
    except Exception as e:
        logger.error(f"Serial Error: {e}")
        return

    car_on_start_time = None
    
    while True:
        if ser.in_waiting >= 9:
            if ser.read() == b'\x59':
                if ser.read() == b'\x59':
                    data = ser.read(7)
                    dist = data[0] + (data[1] << 8)
                    stren = data[2] + (data[3] << 8)
                    
                    with state_lock:
                        state["current_distance"] = dist
                        state["current_strength"] = stren
                        active = state["is_active_by_schedule"] or state["manual_override"]

                    if dist > 0 and stren >= MIN_STRENGTH:
                        if car_on_start_time is None: car_on_start_time = time.time()
                        
                        duration_ms = (time.time() - car_on_start_time) * 1000
                        if duration_ms >= DEBOUNCE_MS and not state["car_present"]:
                            with state_lock: state["car_present"] = True
                            if active: record_detection(dist, stren)
                    else:
                        car_on_start_time = None
                        with state_lock: state["car_present"] = False

def record_detection(dist, stren):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        ts = datetime.now(timezone.utc).isoformat()
        cursor.execute("INSERT INTO detections (timestamp, distance, strength) VALUES (?, ?, ?)", (ts, dist, stren))
        cursor.execute("UPDATE metadata SET value = CAST(value AS INTEGER) + 1 WHERE key = 'total_count'")
        conn.commit()
        conn.close()
        # Trigger MQTT
        threading.Thread(target=mqtt_publish, args=(dist,)).start()
    except Exception as e:
        logger.error(f"DB Error: {e}")

# --- MQTT SYSTEM ---
def mqtt_publish(dist):
    try:
        client = mqtt.Client(MQTT_CFG.get('client_id', 'Orangepi_Lidar'))
        if MQTT_CFG.get('username'):
            client.username_pw_set(MQTT_CFG['username'], MQTT_CFG['password'])
        client.connect(MQTT_CFG['broker'], MQTT_CFG['port'], 60)
        payload = json.dumps({"event": "car_detected", "distance": dist, "ts": datetime.now().isoformat()})
        client.publish(MQTT_CFG['topic'], payload)
        client.disconnect()
    except Exception as e:
        logger.info(f"MQTT Publish skipped/failed: {e}")

# --- WEB ROUTES ---
app = Flask(__name__)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/status')
def get_status():
    with state_lock: return jsonify(state)

@app.route('/api/config', methods=['GET', 'POST'])
def handle_config():
    global cfg
    if request.method == 'POST':
        new_cfg = request.json
        with open(CONFIG_FILE, 'w') as f:
            json.dump(new_cfg, f, indent=4)
        cfg = new_cfg
        return jsonify({"status": "success"})
    return jsonify(cfg)

@app.route('/api/stats/hourly')
def get_hourly_stats():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""SELECT strftime('%H', timestamp, 'localtime') as hour, COUNT(*) 
                      FROM detections 
                      WHERE timestamp >= datetime('now', '-1 day')
                      GROUP BY hour""")
    rows = cursor.fetchall()
    conn.close()
    return jsonify({"labels": [r[0] for r in rows], "values": [r[1] for r in rows]})

# --- SYSTEM UPDATE LOGIC ---
@app.route('/run_update', methods=['POST'])
def run_update():
    upd_cfg = cfg.get("system_update", {})
    repo_path = upd_cfg.get("local_path", BASE_DIR)
    service_name = upd_cfg.get("service_name", "LidarCounter.service")

    try:
        logger.info("Starting System Update...")
        os.chdir(repo_path)
        
        # Pull from GitHub
        subprocess.run(['git', 'fetch', '--all'], check=True)
        subprocess.run(['git', 'reset', '--hard', f'origin/{upd_cfg.get("branch", "main")}'], check=True)
        
        # Optional: Re-install requirements
        if os.path.exists('requirements.txt'):
            subprocess.run(['pip', 'install', '-r', 'requirements.txt'], check=True)

        # Final step: Restart the service
        logger.info("Update complete. Restarting service.")
        subprocess.Popen(["systemctl", "restart", service_name])
        return jsonify({'status': 'success', 'message': 'Update applied. Restarting service...'})
    except Exception as e:
        logger.error(f"Update failed: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/sync_time', methods=['POST'])
def sync_time():
    try:
        subprocess.run(['systemctl', 'stop', 'systemd-timesyncd'], check=True)
        subprocess.run(['ntpsec-ntpdate', '-u', 'time.google.com'], check=True)
        subprocess.run(['systemctl', 'start', 'systemd-timesyncd'], check=True)
        return jsonify({'status': 'success'})
    except:
        return jsonify({'status': 'error'}), 500

if __name__ == '__main__':
    init_db()
    threading.Thread(target=lidar_engine, daemon=True).start()
    threading.Thread(target=check_schedule, daemon=True).start()
    app.run(host=cfg['http']['host'], port=cfg['http']['port'])
