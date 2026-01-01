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

# Logging setup to match your original debug requirements
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# --- LOAD CONFIGURATION ---
def load_config():
    with open(CONFIG_FILE, 'r') as f:
        return json.load(f)

cfg = load_config()

# Hardware specific to Orange Pi Zero 2 UART5
SERIAL_PORT = cfg.get('serial_port', '/dev/ttyS5')
BAUD_RATE = cfg.get('baudrate', 115200)
DB_PATH = os.path.join(BASE_DIR, cfg.get('database', 'cars.db'))

# Detection Settings
DETECTION_CFG = cfg.get('detection', {})
DEBOUNCE_MS = DETECTION_CFG.get('debounce_ms', 200)
MIN_STRENGTH = DETECTION_CFG.get('min_strength', 100)

# MQTT Config
MQTT_CFG = cfg.get('mqtt', {})
MQTT_CLIENT_ID = MQTT_CFG.get('client_id', 'Orangepi_Lidar')

# --- DATABASE SYSTEM ---
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

# --- STATE MANAGEMENT ---
state = {
    "current_distance": 0,
    "current_strength": 0,
    "car_present": False,
    "is_active_by_schedule": False,
    "manual_override": False,
    "test_mode": cfg.get('detection', {}).get('test_mode', False),
    "last_update": None
}
state_lock = threading.Lock()

# --- SCHEDULING SYSTEM ---
def check_schedule():
    """Restored the detailed day-by-day scheduling logic."""
    while True:
        try:
            with open(SCHEDULE_FILE, 'r') as f:
                schedule = json.load(f)
            
            now = datetime.now()
            # Orange Pi weekday: 0=Mon, 6=Sun. Schedule index: 0=Sun, 6=Sat
            weekday_index = (now.weekday() + 1) % 7
            today_sched = schedule[weekday_index]

            if not today_sched.get('Enable', False):
                new_status = False
            else:
                start_time = datetime.strptime(today_sched['StartShow'], "%H:%M").time()
                stop_time = datetime.strptime(today_sched['ShowStop'], "%H:%M").time()
                current_time = now.time()

                if start_time <= stop_time:
                    new_status = start_time <= current_time <= stop_time
                else: # Overnight logic
                    new_status = current_time >= start_time or current_time <= stop_time

            with state_lock:
                state["is_active_by_schedule"] = new_status or state["manual_override"]
            
        except Exception as e:
            logger.error(f"Schedule Check Error: {e}")
        
        time.sleep(30)

# --- LIDAR SENSOR ENGINE ---
def lidar_engine():
    """Direct hex parsing for TFmini Plus on Orange Pi UART5."""
    try:
        ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=0.1)
    except Exception as e:
        logger.error(f"Failed to open Serial Port {SERIAL_PORT}: {e}")
        return

    car_on_start_time = None
    
    while True:
        if ser.in_waiting >= 9:
            if ser.read() == b'\x59':
                if ser.read() == b'\x59':
                    data = ser.read(7)
                    distance = data[0] + (data[1] << 8)
                    strength = data[2] + (data[3] << 8)
                    
                    with state_lock:
                        state["current_distance"] = distance
                        state["current_strength"] = strength
                        active = state["is_active_by_schedule"]

                    # Detection Logic
                    if distance > 0 and strength >= MIN_STRENGTH:
                        if car_on_start_time is None:
                            car_on_start_time = time.time()
                        
                        duration = (time.time() - car_on_start_time) * 1000
                        if duration >= DEBOUNCE_MS and not state["car_present"]:
                            with state_lock:
                                state["car_present"] = True
                            if active:
                                record_detection(distance, strength)
                    else:
                        car_on_start_time = None
                        with state_lock:
                            state["car_present"] = False

def record_detection(dist, stren):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        ts = datetime.now(timezone.utc).isoformat()
        cursor.execute("INSERT INTO detections (timestamp, distance, strength) VALUES (?, ?, ?)", (ts, dist, stren))
        cursor.execute("UPDATE metadata SET value = CAST(value AS INTEGER) + 1 WHERE key = 'total_count'")
        conn.commit()
        conn.close()
        logger.info(f"Car Detected! Distance: {dist}mm")
        # Trigger MQTT
        threading.Thread(target=mqtt_publish, args=(dist,)).start()
    except Exception as e:
        logger.error(f"DB Error: {e}")

# --- MQTT SYSTEM ---
def mqtt_publish(dist):
    try:
        client = mqtt.Client(MQTT_CLIENT_ID)
        if MQTT_CFG.get('username'):
            client.username_pw_set(MQTT_CFG['username'], MQTT_CFG['password'])
        client.connect(MQTT_CFG['broker'], MQTT_CFG['port'])
        payload = json.dumps({"event": "car_detected", "distance": dist, "ts": datetime.now().isoformat()})
        client.publish(MQTT_CFG['topic'], payload)
        client.disconnect()
    except Exception as e:
        logger.error(f"MQTT Error: {e}")

# --- FLASK WEB SERVER ---
app = Flask(__name__)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/status')
def get_status():
    with state_lock:
        return jsonify(state)

@app.route('/api/stats/hourly')
def get_hourly_stats():
    conn = get_db_connection()
    cursor = conn.cursor()
    # Query for the last 24 hours
    cursor.execute("""SELECT strftime('%H', timestamp, 'localtime') as hour, COUNT(*) 
                      FROM detections 
                      WHERE timestamp >= datetime('now', '-1 day')
                      GROUP BY hour""")
    data = cursor.fetchall()
    conn.close()
    return jsonify({"labels": [row[0] for row in data], "values": [row[1] for row in data]})

@app.route('/api/config', methods=['GET', 'POST'])
def handle_config():
    if request.method == 'POST':
        new_cfg = request.json
        with open(CONFIG_FILE, 'w') as f:
            json.dump(new_cfg, f, indent=4)
        return jsonify({"status": "success"})
    return jsonify(load_config())

@app.route('/run_update', methods=['POST'])
def run_update():
    """Full GitHub sync logic."""
    try:
        os.chdir(BASE_DIR)
        subprocess.run(['git', 'fetch', '--all'], check=True)
        subprocess.run(['git', 'reset', '--hard', 'origin/main'], check=True)
        # Restart the service to apply changes
        subprocess.Popen(["systemctl", "restart", "LidarCounter.service"])
        return jsonify({'status': 'success'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

if __name__ == '__main__':
    init_db()
    # Start background threads
    threading.Thread(target=lidar_engine, daemon=True).start()
    threading.Thread(target=check_schedule, daemon=True).start()
    # Run Flask
    app.run(host=cfg['http']['host'], port=cfg['http']['port'])
