#!/usr/bin/env python3

import json
import os
import sqlite3
import csv
import io
import threading
import time
from datetime import datetime, timezone
import subprocess

import requests
import paho.mqtt.client as mqtt
import serial
from flask import Flask, jsonify, request, render_template, send_file, make_response

DEBUG_SENSOR = True

# ----------------- PATHS & CONFIG -----------------
# Root-specific pathing for Orange Pi Zero 2
BASE_DIR = "/root/LidarCounter"
CFG_PATH = os.path.join(BASE_DIR, "config.json")
SCHED_LOCAL_PATH = os.path.join(BASE_DIR, "schedule.json")

if not os.path.exists(CFG_PATH):
    raise SystemExit(f"Missing config.json at {CFG_PATH}")

with open(CFG_PATH, "r") as f:
    cfg = json.load(f)

# Orange Pi Zero 2: UART5 is typically /dev/ttyS5 (Pins 8/10)
SERIAL_PORT = cfg.get("serial_port", "/dev/ttyS5") 
BAUD = cfg.get("baudrate", 115200)
DB_PATH = os.path.join(BASE_DIR, cfg.get("database", "cars.db"))

MQTT_CFG = cfg.get("mqtt", {})
MQTT_BROKER = MQTT_CFG.get("broker", "localhost")
MQTT_PORT = MQTT_CFG.get("port", 1883)
MQTT_TOPIC = MQTT_CFG.get("topic", "carcount/car_detect")
MQTT_USER = MQTT_CFG.get("username") or None
MQTT_PASS = MQTT_CFG.get("password") or None

HTTP_CFG = cfg.get("http", {})
HTTP_HOST = HTTP_CFG.get("host", "0.0.0.0")
HTTP_PORT = int(HTTP_CFG.get("port", 80)) # Port 80 is fine for root user

DETECTION_CFG = cfg.get("detection", {})
DEBOUNCE_MS = int(DETECTION_CFG.get("debounce_ms", 200))
IGNORE_ZERO_DISTANCE = bool(DETECTION_CFG.get("ignore_zero_distance", True))
MIN_STRENGTH = int(DETECTION_CFG.get("min_strength", 0))
TEST_MODE_DEFAULT = bool(DETECTION_CFG.get("test_mode", False))

SCHEDULE_CFG = cfg.get("schedule", {})
SCHEDULE_URL = SCHEDULE_CFG.get("url", "")
GITHUB_TOKEN = SCHEDULE_CFG.get("github_token", "")
SCHEDULE_REFRESH_SECONDS = int(SCHEDULE_CFG.get("check_interval_sec", 1200))

# ----------------- DB -----------------
def init_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS detections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc TEXT NOT NULL,
            distance_mm INTEGER NOT NULL
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT
        );
    """)
    cur.execute("INSERT OR IGNORE INTO metadata (key, value) VALUES (?, ?);", ("total_count", "0"))
    conn.commit()
    return conn

db_conn = init_db()
db_lock = threading.Lock()

def increment_count_and_save(distance_mm: int):
    ts = datetime.now(timezone.utc).isoformat()
    with db_lock:
        cur = db_conn.cursor()
        cur.execute("INSERT INTO detections (ts_utc, distance_mm) VALUES (?, ?);", (ts, int(distance_mm)))
        cur.execute("UPDATE metadata SET value = CAST(value AS INTEGER) + 1 WHERE key = 'total_count';")
        db_conn.commit()

# ... [DB Helper functions remain the same as your source] ...

# ----------------- MQTT -----------------
mqtt_client = mqtt.Client()
if MQTT_USER:
    mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)
try:
    mqtt_client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
    mqtt_client.loop_start()
except Exception as e:
    print("Warning: could not connect to MQTT broker:", e)

def publish_detection(distance_mm: int):
    # Logic same as your source
    now_utc = datetime.now(timezone.utc).isoformat()
    with db_lock:
        cur = db_conn.cursor()
        cur.execute("SELECT COUNT(*) FROM detections WHERE date(ts_utc, 'localtime') = date('now', 'localtime');")
        count_today = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM detections WHERE ts_utc >= datetime('now','localtime','-7 days');")
        count_week = cur.fetchone()[0]
        cur.execute("SELECT value FROM metadata WHERE key='total_count';")
        row = cur.fetchone()
        total_all_time = int(row[0]) if row else 0

    payload = {
        "ts_utc": now_utc,
        "count_today": count_today,
        "count_week": count_week,
        "total_all_time": total_all_time,
        "last_distance_mm": int(distance_mm),
    }
    try:
        mqtt_client.publish(MQTT_TOPIC, json.dumps(payload), qos=1)
    except Exception as e:
        print("MQTT publish failed:", e)

# ----------------- TFmini parsing -----------------
def parse_tfmini_frame(buf: bytes):
    if len(buf) < 9: return None
    if buf[0] != 0x59 or buf[1] != 0x59: return None
    distance = buf[2] + (buf[3] << 8)
    strength = buf[4] + (buf[5] << 8)
    chksum = sum(buf[0:8]) & 0xFF
    if chksum != buf[8]: return None
    return {"distance_mm": distance, "strength": strength}

# ----------------- Global state -----------------
state_lock = threading.Lock()
state = {
    "last_distance_mm": None,
    "last_strength": None,
    "last_valid_reading": False,
    "car_present": False,
    "last_transition_ts": 0,
}
test_mode = TEST_MODE_DEFAULT
test_count_today = 0
manual_override = False
schedule_data = None
schedule_last_fetch_utc = None
schedule_last_status = "Not fetched yet"
schedule_valid = False
schedule_active = False

# ... [Schedule helpers remain the same as your source] ...

def sensor_loop():
    global IGNORE_ZERO_DISTANCE, MIN_STRENGTH, DEBOUNCE_MS, DEBUG_SENSOR
    try:
        ser = serial.Serial(SERIAL_PORT, BAUD, timeout=1)
        dlog(f"Opened serial port {SERIAL_PORT} @ {BAUD}")
    except Exception as e:
        print(f"CRITICAL: Could not open serial port {SERIAL_PORT}: {e}")
        return

    read_buf = bytearray()
    car_active = False
    last_state_is_car = False
    car_since_ms = None
    empty_since_ms = int(time.time() * 1000)

    while True:
        try:
            chunk = ser.read(64)
            if not chunk: continue
            read_buf.extend(chunk)
            while True:
                idx = read_buf.find(b"\x59\x59")
                if idx == -1:
                    if len(read_buf) > 2: read_buf = read_buf[-2:]
                    break
                if idx > 0: read_buf = read_buf[idx:]
                if len(read_buf) < 9: break
                frame = bytes(read_buf[:9]); read_buf = read_buf[9:]
                parsed = parse_tfmini_frame(frame)
                if parsed is None: continue

                distance_mm = parsed["distance_mm"]
                strength = parsed["strength"]
                now_ms = int(time.time() * 1000)
                valid_reading = not (IGNORE_ZERO_DISTANCE and distance_mm == 0) and not (MIN_STRENGTH and strength < MIN_STRENGTH)
                is_car_sample = valid_reading and distance_mm > 0

                with state_lock:
                    state["last_distance_mm"] = distance_mm
                    state["last_strength"] = strength
                    state["last_valid_reading"] = valid_reading
                    sched_ok = schedule_active or manual_override
                    tm = test_mode

                if is_car_sample:
                    if not last_state_is_car: car_since_ms = now_ms
                    last_state_is_car = True
                    if not car_active and car_since_ms is not None:
                        if now_ms - car_since_ms >= DEBOUNCE_MS:
                            car_active = True
                            with state_lock: state["car_present"] = True
                            if not sched_ok:
                                dlog("Car detected but schedule inactive.")
                                continue
                            dlog("Car DETECTED and recorded.")
                            threading.Thread(target=handle_detection, args=(distance_mm, not tm), daemon=True).start()
                else:
                    if last_state_is_car: empty_since_ms = now_ms
                    last_state_is_car = False
                    if car_active and empty_since_ms is not None:
                        if now_ms - empty_since_ms >= DEBOUNCE_MS:
                            car_active = False
                with state_lock: state["car_present"] = car_active
        except Exception as e:
            print("Serial read error:", e)
            time.sleep(0.5)

# ----------------- Flask app -----------------
app = Flask(__name__, template_folder="templates")

# ... [Route: index, config, schedule, status, config, mode, stats same as your source] ...

@app.route("/api/service/restart", methods=["POST"])
def restart_service():
    try:
        # Standardized service name, no sudo needed
        subprocess.Popen(["systemctl", "restart", "LidarCounter.service"], stdout=subprocess.DEVNULL)
        return {"ok": True}
    except Exception as e: return {"ok": False, "error": str(e)}, 500

@app.route('/sync_time', methods=['POST'])
def sync_time():
    try:
        # Direct calls as root
        subprocess.run(['systemctl', 'stop', 'systemd-timesyncd'], check=True)
        subprocess.run(['ntpdate', '-u', 'time.google.com'], check=True) # ntpdate is common on Armbian
        subprocess.run(['systemctl', 'start', 'systemd-timesyncd'], check=True)
        return jsonify({'status': 'success', 'message': 'Time synced!'})
    except Exception as e:
        subprocess.run(['systemctl', 'start', 'systemd-timesyncd'])
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/run_update', methods=['POST'])
def run_update():
    try:
        # Pull logic specifically for Orange Pi Zero 2 root dir
        repo_path = '/root/LidarCounter'
        os.chdir(repo_path)
        subprocess.run(['git', 'fetch', '--all'], check=True)
        subprocess.run(['git', 'reset', '--hard', 'origin/main'], check=True)
        # Restart the specific service name
        subprocess.Popen(["systemctl", "restart", "LidarCounter.service"], stdout=subprocess.DEVNULL)
        return jsonify({'status': 'success', 'message': 'System updated!'})
    except Exception as e: return jsonify({'status': 'error', 'message': str(e)}), 500

# ... [download_db and download_csv same as source] ...

if __name__ == "__main__":
    threading.Thread(target=sensor_loop, daemon=True).start()
    threading.Thread(target=schedule_loop, daemon=True).start()
    threading.Thread(target=schedule_eval_loop, daemon=True).start()
    app.run(host=HTTP_HOST, port=HTTP_PORT, debug=False)
