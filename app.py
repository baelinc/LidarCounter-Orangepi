#!/usr/bin/env python3
"""                                                                                                    
          ░██████   ░██     ░██   ░██████   ░██        ░██ ░███      ░███   ░██████   ░███     ░██ 
         ░██   ░██  ░██     ░██   ░██   ░██  ░██        ░██ ░████    ░████  ░██   ░██  ░████    ░██ 
        ░██         ░██     ░██ ░██     ░██ ░██   ░██   ░██ ░██░██ ░██░██ ░██     ░██ ░██░██   ░██ 
         ░████████  ░██████████ ░██     ░██ ░██  ░████  ░██ ░██ ░████ ░██ ░██     ░██ ░██ ░██  ░██ 
                ░██ ░██     ░██ ░██     ░██ ░██░██ ░██░██ ░██  ░██  ░██ ░██     ░██ ░██  ░██░██ 
         ░██   ░██  ░██     ░██  ░██   ░██  ░████   ░████ ░██        ░██  ░██   ░██  ░██   ░████ 
          ░██████   ░██     ░██   ░██████   ░███     ░███ ░██        ░██   ░██████   ░██    ░███ 

Light Show Network ShowMon Lidar Car Counter - Orange Pi Edition (Root)
v1.0
"""

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
# Adjusted for root login installation
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(BASE_DIR, "config.json")
SCHED_LOCAL_PATH = os.path.join(BASE_DIR, "schedule.json")

if not os.path.exists(CFG_PATH):
    raise SystemExit(f"Missing config.json at {CFG_PATH}")

with open(CFG_PATH, "r") as f:
    cfg = json.load(f)

# --- Top-level config ---
# Orange Pi usually uses /dev/ttyS0 or /dev/ttyS3 for UART
SERIAL_PORT = cfg.get("serial_port", "/dev/ttyS0") 
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
HTTP_PORT = int(HTTP_CFG.get("port", 80))

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
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS detections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc TEXT NOT NULL,
            distance_mm INTEGER NOT NULL
        );
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """
    )
    cur.execute(
        "INSERT OR IGNORE INTO metadata (key, value) VALUES (?, ?);",
        ("total_count", "0"),
    )
    conn.commit()
    return conn

db_conn = init_db()
db_lock = threading.Lock()

def increment_count_and_save(distance_mm: int):
    ts = datetime.now(timezone.utc).isoformat()
    with db_lock:
        cur = db_conn.cursor()
        cur.execute(
            "INSERT INTO detections (ts_utc, distance_mm) VALUES (?, ?);",
            (ts, int(distance_mm)),
        )
        cur.execute(
            "UPDATE metadata SET value = CAST(value AS INTEGER) + 1 WHERE key = 'total_count';"
        )
        db_conn.commit()

def get_total_count() -> int:
    with db_lock:
        cur = db_conn.cursor()
        cur.execute("SELECT value FROM metadata WHERE key = 'total_count';")
        row = cur.fetchone()
        return int(row[0]) if row else 0

def reset_total_count():
    with db_lock:
        cur = db_conn.cursor()
        cur.execute("UPDATE metadata SET value = 0 WHERE key='total_count';")
        db_conn.commit()

def wipe_database():
    with db_lock:
        cur = db_conn.cursor()
        cur.execute("DELETE FROM detections;")
        cur.execute("UPDATE metadata SET value = 0 WHERE key='total_count';")
        db_conn.commit()

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

# ----------------- Schedule helpers -----------------
def compute_schedule_flags(now_local: datetime):
    global schedule_data
    with state_lock:
        data = schedule_data
    if not data: return False, False, False
    try:
        weekday = now_local.weekday() 
        idx = (weekday + 1) % 7 
        entry = data[idx]
        enable = bool(entry.get("Enable", False))
        start_str = entry.get("StartShow")
        stop_str = entry.get("ShowStop")
        if not start_str or not stop_str: return False, False, enable
        start_t = datetime.strptime(start_str, "%H:%M").time()
        stop_t = datetime.strptime(stop_str, "%H:%M").time()
    except Exception: return False, False, False

    now_t = now_local.time()
    if start_t <= stop_t:
        active = start_t <= now_t <= stop_t
    else:
        active = now_t >= start_t or now_t <= stop_t
    return True, active, enable

def refresh_schedule():
    global schedule_data, schedule_last_fetch_utc, schedule_last_status
    url = SCHEDULE_URL
    if not url:
        with state_lock:
            schedule_last_fetch_utc = datetime.now(timezone.utc).isoformat()
            schedule_last_status = "No schedule URL configured"
        return False
    headers = {"Authorization": f"token {GITHUB_TOKEN}"} if GITHUB_TOKEN else {}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list) or len(data) != 7:
            raise ValueError("Schedule JSON must be a list of 7 entries")
        with state_lock:
            globals()["schedule_data"] = data
            globals()["schedule_last_fetch_utc"] = datetime.now(timezone.utc).isoformat()
            globals()["schedule_last_status"] = "OK"
        try:
            with open(SCHED_LOCAL_PATH, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print("Warning: could not write local schedule.json:", e)
        
        now_local = datetime.now()
        valid, active, enabled = compute_schedule_flags(now_local)
        with state_lock:
            globals()["schedule_valid"] = valid
            globals()["schedule_active"] = (True if manual_override else (valid and enabled and active))
        return True
    except Exception as e:
        with state_lock:
            globals()["schedule_last_fetch_utc"] = datetime.now(timezone.utc).isoformat()
            globals()["schedule_last_status"] = f"Error: {e}"
        return False

def load_local_schedule():
    global schedule_data, schedule_last_fetch_utc, schedule_last_status
    if not os.path.exists(SCHED_LOCAL_PATH): return False
    try:
        with open(SCHED_LOCAL_PATH, "r") as f:
            data = json.load(f)
        with state_lock:
            globals()["schedule_data"] = data
            globals()["schedule_last_fetch_utc"] = datetime.now(timezone.utc).isoformat()
            globals()["schedule_last_status"] = "Loaded from local schedule.json"
        return True
    except Exception as e:
        print("Error loading local schedule:", e)
        return False

def schedule_loop():
    refresh_schedule()
    while True:
        time.sleep(SCHEDULE_REFRESH_SECONDS)
        refresh_schedule()

# ----------------- Sensor loop -----------------
def dlog(msg):
    if DEBUG_SENSOR: print("DEBUG:", msg)

def sensor_loop():
    global IGNORE_ZERO_DISTANCE, MIN_STRENGTH, DEBOUNCE_MS, DEBUG_SENSOR
    try:
        ser = serial.Serial(SERIAL_PORT, BAUD, timeout=1)
        dlog(f"Opened serial port {SERIAL_PORT} @ {BAUD}")
    except Exception as e:
        print(f"Could not open serial port {SERIAL_PORT}:", e)
        return

    read_buf = bytearray()
    car_active = False
    last_state_is_car = False
    car_since_ms = None
    empty_since_ms = int(time.time() * 1000)
    dlog("Sensor loop started...")

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
                            if not sched_ok:
                                dlog("*** CAR DETECTED but schedule inactive — IGNORING ***")
                                car_active = True
                                with state_lock: state["car_present"] = True
                                continue
                            dlog("*** CAR DETECTED! Counting car ***")
                            car_active = True
                            threading.Thread(target=handle_detection, args=(distance_mm, not tm), daemon=True).start()
                else:
                    if last_state_is_car: empty_since_ms = now_ms
                    last_state_is_car = False
                    if car_active and empty_since_ms is not None:
                        if now_ms - empty_since_ms >= DEBOUNCE_MS:
                            dlog("Car ended — empty state stable")
                            car_active = False
                with state_lock: state["car_present"] = car_active
        except Exception as e:
            print("Serial read error:", e)
            time.sleep(0.5)

def handle_detection(distance_mm: int, log_to_db: bool):
    global test_count_today
    if log_to_db:
        try: increment_count_and_save(distance_mm)
        except Exception as e: print("DB insert error:", e)
    else:
        with state_lock: test_count_today += 1
    try: publish_detection(distance_mm)
    except Exception as e: print("MQTT publish error:", e)

def schedule_eval_loop():
    global schedule_valid, schedule_active
    while True:
        try:
            now_local = datetime.now()
            valid, active, enabled = compute_schedule_flags(now_local)
            with state_lock:
                schedule_valid = valid
                schedule_active = True if manual_override else (valid and enabled and active)
        except Exception as e: print("Schedule eval error:", e)
        time.sleep(60)

# ----------------- Flask app -----------------
app = Flask(__name__, template_folder="templates")

@app.route("/")
def index(): return render_template("index.html")

@app.route("/config")
def config_page(): return render_template("config.html")

@app.route("/schedule")
def schedule_page(): return render_template("schedule.html")

@app.route("/api/status", methods=["GET"])
def api_status():
    with state_lock:
        return jsonify({
            "last_distance_mm": state.get("last_distance_mm"),
            "last_strength": state.get("last_strength"),
            "last_valid_reading": state.get("last_valid_reading", False),
            "car_present": bool(state.get("car_present")),
            "schedule_active": bool(schedule_active),
            "schedule_valid": bool(schedule_valid),
            "schedule_last_fetch_utc": schedule_last_fetch_utc,
            "schedule_last_status": schedule_last_status,
            "test_mode": test_mode,
            "manual_override": manual_override,
        })

@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    global cfg, SCHEDULE_URL, GITHUB_TOKEN, SCHEDULE_REFRESH_SECONDS, DEBOUNCE_MS, IGNORE_ZERO_DISTANCE, MIN_STRENGTH, test_mode
    if request.method == "GET": return jsonify(cfg)
    data = request.json or {}
    for k in ["serial_port", "baudrate", "database"]:
        if k in data: cfg[k] = data[k]
    if "mqtt" in data: cfg.setdefault("mqtt", {}).update(data["mqtt"])
    if "http" in data: cfg.setdefault("http", {}).update(data["http"])
    if "detection" in data: cfg.setdefault("detection", {}).update(data["detection"])
    if "schedule" in data: cfg.setdefault("schedule", {}).update(data["schedule"])
    with open(CFG_PATH, "w") as f: json.dump(cfg, f, indent=2)
    det = cfg.get("detection", {})
    DEBOUNCE_MS = int(det.get("debounce_ms", DEBOUNCE_MS))
    IGNORE_ZERO_DISTANCE = bool(det.get("ignore_zero_distance", IGNORE_ZERO_DISTANCE))
    MIN_STRENGTH = int(det.get("min_strength", MIN_STRENGTH))
    if "test_mode" in det:
        with state_lock: test_mode = bool(det["test_mode"])
    return jsonify({"ok": True, "config": cfg})

@app.route("/api/mode", methods=["GET", "POST"])
def api_mode():
    global test_mode, test_count_today, manual_override
    if request.method == "GET":
        with state_lock: return jsonify({"test_mode": bool(test_mode), "manual_override": bool(manual_override)})
    data = request.json or {}
    with state_lock:
        if "test_mode" in data:
            if bool(data["test_mode"]) and not test_mode: test_count_today = 0
            test_mode = bool(data["test_mode"])
        if "manual_override" in data: manual_override = bool(data["manual_override"])
    cfg.setdefault("detection", {})["test_mode"] = test_mode
    with open(CFG_PATH, "w") as f: json.dump(cfg, f, indent=2)
    return jsonify({"ok": True, "test_mode": test_mode, "manual_override": manual_override})

@app.route("/api/stats", methods=["GET"])
def api_stats():
    with db_lock:
        cur = db_conn.cursor()
        cur.execute("SELECT COUNT(*) FROM detections WHERE date(ts_utc,'localtime') = date('now','localtime');")
        today_db = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM detections WHERE date(ts_utc,'localtime') = date('now','localtime','-1 day');")
        yesterday = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM detections WHERE ts_utc >= datetime('now','localtime','-7 days');")
        week = cur.fetchone()[0]
        cur.execute("SELECT value FROM metadata WHERE key='total_count';")
        total_all_time = int(cur.fetchone()[0])
    with state_lock: current = test_count_today if test_mode else today_db
    return jsonify({"current_count": current, "yesterday_count": yesterday, "week_count": week, "total_all_time": total_all_time, "test_mode": test_mode})

@app.route("/api/service/restart", methods=["POST"])
def restart_service():
    try:
        # Removed sudo -n as we are running as root
        subprocess.Popen(["/bin/systemctl", "restart", "--no-block", "ShowMonLidarCounter"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"ok": True}
    except Exception as e: return {"ok": False, "error": str(e)}, 500

@app.route('/sync_time', methods=['POST'])
def sync_time():
    try:
        # Commands simplified for root (no sudo needed)
        subprocess.run(['systemctl', 'stop', 'systemd-timesyncd'], check=True)
        subprocess.run(['ntpsec-ntpdate', '-u', 'time.google.com'], check=True)
        subprocess.run(['systemctl', 'start', 'systemd-timesyncd'], check=True)
        return jsonify({'status': 'success', 'message': 'Time synchronized successfully!'})
    except Exception as e:
        subprocess.run(['systemctl', 'start', 'systemd-timesyncd'])
        return jsonify({'status': 'error', 'message': f'Sync failed: {str(e)}'}), 500

@app.route('/run_update', methods=['POST'])
def run_update():
    try:
        with open(CFG_PATH, 'r') as f: config = json.load(f)
        update_cfg = config.get('system_update', {})
        repo_url = update_cfg.get('repo_url')
        branch = update_cfg.get('branch', 'main')
        if not repo_url: return jsonify({'status': 'error', 'message': 'repo_url not found'}), 400

        # Updated path to root directory
        repo_path = '/root/LidarCounter'
        os.chdir(repo_path)
        subprocess.run(['git', 'remote', 'set-url', 'origin', repo_url], check=True)
        subprocess.run(['git', 'fetch', '--all'], check=True)
        subprocess.run(['git', 'reset', '--hard', f'origin/{branch}'], check=True)
        subprocess.run(['systemctl', 'restart', 'ShowMonLidarCounter.service'], check=False)
        return jsonify({'status': 'success', 'message': 'System updated!'})
    except Exception as e: return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/download_db')
def download_db():
    if os.path.exists(DB_PATH): return send_file(DB_PATH, as_attachment=True)
    return "Database file not found.", 404

@app.route('/download_csv')
def download_csv():
    if not os.path.exists(DB_PATH): return "Database not found.", 404
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='detections';")
        table = cursor.fetchone()
        if not table: return "Detections table not found.", 404
        cursor.execute("SELECT * FROM detections")
        rows = cursor.fetchall()
        column_names = [d[0] for d in cursor.description]
        si = io.StringIO()
        cw = csv.writer(si)
        cw.writerow(column_names)
        cw.writerows(rows)
        output = make_response(si.getvalue())
        output.headers["Content-Disposition"] = "attachment; filename=detections.csv"
        output.headers["Content-type"] = "text/csv"
        return output
    finally: conn.close()

if __name__ == "__main__":
    # Start threads
    threading.Thread(target=sensor_loop, daemon=True).start()
    threading.Thread(target=schedule_loop, daemon=True).start()
    threading.Thread(target=schedule_eval_loop, daemon=True).start()
    # Run Flask
    app.run(host=HTTP_HOST, port=HTTP_PORT, debug=False)
