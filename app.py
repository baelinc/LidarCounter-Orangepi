#!/usr/bin/env python3
"""                                                                                                   
          ░██████   ░██     ░██   ░██████   ░██       ░██ ░███     ░███   ░██████   ░███    ░██ 
         ░██   ░██  ░██     ░██  ░██   ░██  ░██       ░██ ░████   ░████  ░██   ░██  ░████   ░██ 
        ░██         ░██     ░██ ░██     ░██ ░██  ░██  ░██ ░██░██ ░██░██ ░██     ░██ ░██░██  ░██ 
         ░████████  ░██████████ ░██     ░██ ░██ ░████ ░██ ░██ ░████ ░██ ░██     ░██ ░██ ░██ ░██ 
                ░██ ░██     ░██ ░██     ░██ ░██░██ ░██░██ ░██  ░██  ░██ ░██     ░██ ░██  ░██░██ 
         ░██   ░██  ░██     ░██  ░██   ░██  ░████   ░████ ░██       ░██  ░██   ░██  ░██   ░████ 
          ░██████   ░██     ░██   ░██████   ░███     ░███ ░██       ░██   ░██████   ░██    ░███ 


Light Show Network ShowMon Lidar Car Counter
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
from datetime import datetime

DEBUG_SENSOR = True

# ----------------- PATHS & CONFIG -----------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(BASE_DIR, "config.json")
SCHED_LOCAL_PATH = os.path.join(BASE_DIR, "schedule.json")

if not os.path.exists(CFG_PATH):
    raise SystemExit("Missing config.json")

with open(CFG_PATH, "r") as f:
    cfg = json.load(f)

# --- Top-level config ---
SERIAL_PORT = cfg.get("serial_port", "/dev/serial0")
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
        # Today (local)
        cur.execute(
            "SELECT COUNT(*) FROM detections "
            "WHERE date(ts_utc, 'localtime') = date('now', 'localtime');"
        )
        count_today = cur.fetchone()[0]

        # Last 7 days
        cur.execute(
            "SELECT COUNT(*) FROM detections "
            "WHERE ts_utc >= datetime('now','localtime','-7 days');"
        )
        count_week = cur.fetchone()[0]

        # All time
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
    """
    TFmini/TFmini-Plus UART frame:
    0: 0x59
    1: 0x59
    2: dist_L
    3: dist_H
    4: strength_L
    5: strength_H
    6: temp_L
    7: temp_H
    8: checksum = sum(bytes[0:8]) & 0xFF
    """
    if len(buf) < 9:
        return None
    if buf[0] != 0x59 or buf[1] != 0x59:
        return None
    dist_l = buf[2]
    dist_h = buf[3]
    distance = dist_l + (dist_h << 8)
    strength = buf[4] + (buf[5] << 8)
    chksum = sum(buf[0:8]) & 0xFF
    if chksum != buf[8]:
        return None
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

# test mode and manual override
test_mode = TEST_MODE_DEFAULT
test_count_today = 0
manual_override = False

# schedule
schedule_data = None  # list of 7 entries or None
schedule_last_fetch_utc = None
schedule_last_status = "Not fetched yet"
schedule_valid = False
schedule_active = False

# ----------------- Schedule helpers -----------------
def compute_schedule_flags(now_local: datetime):
    """
    Returns (valid, active, enable) for the given local time.
    - valid: schedule entry is present and times parse
    - active: now between StartShow and ShowStop (including overnight wrap)
    - enable: entry.Enable is True
    """
    global schedule_data

    with state_lock:
        data = schedule_data

    if not data:
        return False, False, False

    try:
        # Python: Monday = 0 ... Sunday = 6
        # Schedule: Sunday index 0 ... Saturday = 6
        weekday = now_local.weekday()  # 0=Mon..6=Sun
        idx = (weekday + 1) % 7  # map Mon->1, ..., Sun->0

        entry = data[idx]
        enable = bool(entry.get("Enable", False))
        start_str = entry.get("StartShow")
        stop_str = entry.get("ShowStop")

        if not start_str or not stop_str:
            return False, False, enable

        start_t = datetime.strptime(start_str, "%H:%M").time()
        stop_t = datetime.strptime(stop_str, "%H:%M").time()
    except Exception:
        return False, False, False

    now_t = now_local.time()

    # support overnight ranges (e.g. 23:00–02:00)
    if start_t <= stop_t:
        active = start_t <= now_t <= stop_t
    else:
        active = now_t >= start_t or now_t <= stop_t

    return True, active, enable


def refresh_schedule():
    """
    Fetch schedule JSON from GitHub (or other URL) and update schedule_data.
    Also writes to local schedule.json if successful.
    """
    global schedule_data, schedule_last_fetch_utc, schedule_last_status

    url = SCHEDULE_URL
    if not url:
        with state_lock:
            schedule_last_fetch_utc = datetime.now(timezone.utc).isoformat()
            schedule_last_status = "No schedule URL configured"
        return False

    headers = {}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"token {GITHUB_TOKEN}"

    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list) or len(data) != 7:
            raise ValueError("Schedule JSON must be a list of 7 entries")

        # Store schedule
        with state_lock:
            globals()["schedule_data"] = data
            globals()["schedule_last_fetch_utc"] = datetime.now(
                timezone.utc
            ).isoformat()
            globals()["schedule_last_status"] = "OK"

        # Write local copy
        try:
            with open(SCHED_LOCAL_PATH, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print("Warning: could not write local schedule.json:", e)
            
        # schedule evaluation
        try:
            now_local = datetime.now()
            valid, active, enabled = compute_schedule_flags(now_local)
            with state_lock:
                globals()["schedule_valid"] = valid
                globals()["schedule_active"] = (
                    True if manual_override else (valid and enabled and active)
                )
        except Exception as e:
            print("Immediate schedule eval failed:", e)

        return True

    except Exception as e:
        with state_lock:
            globals()["schedule_last_fetch_utc"] = datetime.now(
                timezone.utc
            ).isoformat()
            globals()["schedule_last_status"] = f"Error: {e}"
        return False
    

def load_local_schedule():
    """
    Load schedule from local schedule.json and update schedule_data.
    Used by /api/schedule/local.
    """
    global schedule_data, schedule_last_fetch_utc, schedule_last_status

    if not os.path.exists(SCHED_LOCAL_PATH):
        return False

    try:
        with open(SCHED_LOCAL_PATH, "r") as f:
            data = json.load(f)
        if not isinstance(data, list) or len(data) != 7:
            raise ValueError("schedule.json must be a list of 7 entries")

        with state_lock:
            globals()["schedule_data"] = data
            globals()["schedule_last_fetch_utc"] = datetime.now(
                timezone.utc
            ).isoformat()
            globals()["schedule_last_status"] = "Loaded from local schedule.json"

        return True
    except Exception as e:
        print("Error loading local schedule:", e)
        return False


def schedule_loop():
    # initial fetch
    refresh_schedule()
    while True:
        time.sleep(SCHEDULE_REFRESH_SECONDS)
        refresh_schedule()

# ----------------- Sensor loop -----------------
def dlog(msg):
    if DEBUG_SENSOR:
        print("DEBUG:", msg)


def sensor_loop():
    global IGNORE_ZERO_DISTANCE, MIN_STRENGTH, DEBOUNCE_MS, DEBUG_SENSOR

    try:
        ser = serial.Serial(SERIAL_PORT, BAUD, timeout=1)
        dlog(f"Opened serial port {SERIAL_PORT} @ {BAUD}")
    except Exception as e:
        print("Could not open serial port:", e)
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
            if not chunk:
                continue
            read_buf.extend(chunk)

            while True:
                idx = read_buf.find(b"\x59\x59")
                if idx == -1:
                    if len(read_buf) > 2:
                        read_buf = read_buf[-2:]
                    break

                if idx > 0:
                    read_buf = read_buf[idx:]

                if len(read_buf) < 9:
                    break

                frame = bytes(read_buf[:9])
                read_buf = read_buf[9:]

                parsed = parse_tfmini_frame(frame)
                if parsed is None:
                    continue

                distance_mm = parsed["distance_mm"]
                strength = parsed["strength"]
                now_ms = int(time.time() * 1000)

                # ---- Classify sample ----
                valid_reading = True
                if IGNORE_ZERO_DISTANCE and distance_mm == 0:
                    valid_reading = False
                if MIN_STRENGTH and strength < MIN_STRENGTH:
                    valid_reading = False

                # "Car sample" = any valid sample with non-zero distance
                is_car_sample = valid_reading and distance_mm > 0

                # ---- Update state for API ----
                with state_lock:
                    state["last_distance_mm"] = distance_mm
                    state["last_strength"] = strength
                    state["last_valid_reading"] = valid_reading

                    sched_ok = schedule_active or manual_override
                    tm = test_mode

                # ---- Debounce logic ----
                if is_car_sample:
                    if not last_state_is_car:
                        car_since_ms = now_ms
                    last_state_is_car = True

                    if not car_active and car_since_ms is not None:
                        if now_ms - car_since_ms >= DEBOUNCE_MS:

                            # Enforce schedule BEFORE counting
                            if not sched_ok:
                                dlog(
                                    "*** CAR DETECTED but schedule inactive — IGNORING ***"
                                )
                                car_active = True  # latch but do NOT count
                                with state_lock:
                                    state["car_present"] = True
                                continue

                            dlog("*** CAR DETECTED! Counting car now ***")
                            car_active = True

                            threading.Thread(
                                target=handle_detection,
                                args=(distance_mm, not tm),  # log only if not test mode
                                daemon=True,
                            ).start()

                else:
                    if last_state_is_car:
                        empty_since_ms = now_ms
                    last_state_is_car = False

                    if car_active and empty_since_ms is not None:
                        if now_ms - empty_since_ms >= DEBOUNCE_MS:
                            dlog("Car ended — empty state stable")
                            car_active = False

                with state_lock:
                    state["car_present"] = car_active

        except Exception as e:
            print("Serial read error:", e)
            time.sleep(0.5)


def handle_detection(distance_mm: int, log_to_db: bool):
    global test_count_today

    if log_to_db:
        try:
            increment_count_and_save(distance_mm)
        except Exception as e:
            print("DB insert error:", e)
    else:
        # Test mode: keep a local counter only
        try:
            with state_lock:
                test_count_today += 1
        except Exception as e:
            print("Test counter error:", e)

    # Always publish MQTT (even in test mode)
    try:
        publish_detection(distance_mm)
    except Exception as e:
        print("MQTT publish error:", e)

# ----------------- SCHEDULE EVAL LOOP -----------------
def schedule_eval_loop():
    """Recompute schedule_active & schedule_valid once per minute."""
    global schedule_valid, schedule_active

    while True:
        try:
            now_local = datetime.now()  # Pi is assumed to keep local timezone
            valid, active, enabled = compute_schedule_flags(now_local)

            with state_lock:
                schedule_valid = valid
                if manual_override:
                    schedule_active = True
                else:
                    schedule_active = valid and enabled and active
        except Exception as e:
            print("Schedule eval error:", e)

        time.sleep(60)  # recompute every minute

# ----------------- Flask app -----------------
app = Flask(__name__, template_folder="templates")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/config")
def config_page():
    return render_template("config.html")


@app.route("/schedule")
def schedule_page():
    return render_template("schedule.html")


@app.route("/api/status", methods=["GET"])
def api_status():
    with state_lock:
        last_distance = state.get("last_distance_mm")
        last_strength = state.get("last_strength")
        present = state.get("car_present")
        last_valid = state.get("last_valid_reading", False)
        sched_active = schedule_active
        sched_valid = schedule_valid
        last_fetch = schedule_last_fetch_utc
        last_status = schedule_last_status
        tm = test_mode
        mo = manual_override
    return jsonify(
        {
            "last_distance_mm": last_distance,
            "last_strength": last_strength,
            "last_valid_reading": last_valid,
            "car_present": bool(present),
            "schedule_active": bool(sched_active),
            "schedule_valid": bool(sched_valid),
            "schedule_last_fetch_utc": last_fetch,
            "schedule_last_status": last_status,
            "test_mode": tm,
            "manual_override": mo,
        }
    )

# ------------- CONFIG API -------------
@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    global cfg, SCHEDULE_URL, GITHUB_TOKEN, SCHEDULE_REFRESH_SECONDS
    global DEBOUNCE_MS, IGNORE_ZERO_DISTANCE, MIN_STRENGTH, test_mode

    if request.method == "GET":
        # Return the full config.json contents
        return jsonify(cfg)

    data = request.json or {}

    # Top-level simple keys
    for k in ["serial_port", "baudrate", "database"]:
        if k in data:
            cfg[k] = data[k]

    # MQTT
    if "mqtt" in data and isinstance(data["mqtt"], dict):
        if "mqtt" not in cfg or not isinstance(cfg["mqtt"], dict):
            cfg["mqtt"] = {}
        cfg["mqtt"].update(data["mqtt"])

    # HTTP
    if "http" in data and isinstance(data["http"], dict):
        if "http" not in cfg or not isinstance(cfg["http"], dict):
            cfg["http"] = {}
        cfg["http"].update(data["http"])

    # Detection
    if "detection" in data and isinstance(data["detection"], dict):
        if "detection" not in cfg or not isinstance(cfg["detection"], dict):
            cfg["detection"] = {}
        cfg["detection"].update(data["detection"])

    # Schedule
    if "schedule" in data and isinstance(data["schedule"], dict):
        if "schedule" not in cfg or not isinstance(cfg["schedule"], dict):
            cfg["schedule"] = {}
        cfg["schedule"].update(data["schedule"])

    # Persist back to file
    with open(CFG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)

    # Apply selected runtime fields
    det = cfg.get("detection", {})
    DEBOUNCE_MS = int(det.get("debounce_ms", DEBOUNCE_MS))
    IGNORE_ZERO_DISTANCE = bool(det.get("ignore_zero_distance", IGNORE_ZERO_DISTANCE))
    MIN_STRENGTH = int(det.get("min_strength", MIN_STRENGTH))
    if "test_mode" in det:
        with state_lock:
            test_mode = bool(det["test_mode"])

    sch = cfg.get("schedule", {})
    SCHEDULE_URL = sch.get("url", SCHEDULE_URL)
    GITHUB_TOKEN = sch.get("github_token", GITHUB_TOKEN)
    SCHEDULE_REFRESH_SECONDS = int(sch.get("check_interval_sec", SCHEDULE_REFRESH_SECONDS))

    return jsonify({"ok": True, "config": cfg})

# ------------- MODE API -------------
@app.route("/api/mode", methods=["GET", "POST"])
def api_mode():
    global test_mode, test_count_today, manual_override

    if request.method == "GET":
        with state_lock:
            return jsonify(
                {"test_mode": bool(test_mode), "manual_override": bool(manual_override)}
            )

    data = request.json or {}
    with state_lock:
        if "test_mode" in data:
            new_tm = bool(data["test_mode"])
            if new_tm and not test_mode:
                test_count_today = 0
            test_mode = new_tm
        if "manual_override" in data:
            manual_override = bool(data["manual_override"])

        tm = test_mode
        mo = manual_override

    # Also update cfg.detection.test_mode for persistence
    if "detection" not in cfg:
        cfg["detection"] = {}
    cfg["detection"]["test_mode"] = tm
    with open(CFG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)

    return jsonify({"ok": True, "test_mode": tm, "manual_override": mo})

# ------------- SCHEDULE APIs -------------
@app.route("/api/schedule/refresh", methods=["POST"])
def api_schedule_refresh():
    ok = refresh_schedule()
    with state_lock:
        last_fetch = schedule_last_fetch_utc
        last_status = schedule_last_status
    return jsonify(
        {
            "ok": ok,
            "schedule_last_fetch_utc": last_fetch,
            "schedule_last_status": last_status,
        }
    )


@app.route("/api/schedule/local", methods=["GET", "POST"])
def api_schedule_local():
    global schedule_data, schedule_last_fetch_utc, schedule_last_status

    if request.method == "GET":
        # Prefer in-memory schedule if present
        with state_lock:
            data = schedule_data

        if data is None:
            # Try to load from local file
            if not load_local_schedule():
                # Return a default blank schedule
                default = []
                for _ in range(7):
                    default.append(
                        {
                            "Enable": False,
                            "StartShow": "16:30",
                            "ShowStop": "22:00"
                        }
                    )
                return jsonify(default)

            with state_lock:
                data = schedule_data

        return jsonify(data)

    # POST: Save new schedule locally and apply immediately
    new_sched = request.json
    if not isinstance(new_sched, list) or len(new_sched) != 7:
        return jsonify({"ok": False, "error": "Schedule must be a list of 7 entries"}), 400

    # Basic validation
    for i, entry in enumerate(new_sched):
        if not isinstance(entry, dict):
            return jsonify({"ok": False, "error": f"Entry {i} must be an object"}), 400
        if "Enable" not in entry or "StartShow" not in entry or "ShowStop" not in entry:
            return jsonify(
                {
                    "ok": False,
                    "error": f"Entry {i} must include Enable, StartShow, ShowStop",
                }
            ), 400

    # Save to disk
    try:
        with open(SCHED_LOCAL_PATH, "w") as f:
            json.dump(new_sched, f, indent=2)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    # Update in-memory
    with state_lock:
        globals()["schedule_data"] = new_sched
        globals()["schedule_last_fetch_utc"] = datetime.now(
            timezone.utc
        ).isoformat()
        globals()["schedule_last_status"] = "Updated via local editor"

    return jsonify({"ok": True})

# ------------- STATS APIs -------------
@app.route("/api/stats", methods=["GET"])
def api_stats():
    # DB-based stats
    with db_lock:
        cur = db_conn.cursor()
        # today (local time)
        cur.execute(
            "SELECT COUNT(*) FROM detections WHERE "
            "date(ts_utc,'localtime') = date('now','localtime');"
        )
        today_db = cur.fetchone()[0]

        # yesterday
        cur.execute(
            "SELECT COUNT(*) FROM detections WHERE "
            "date(ts_utc,'localtime') = date('now','localtime','-1 day');"
        )
        yesterday = cur.fetchone()[0]

        # last 7 days
        cur.execute(
            "SELECT COUNT(*) FROM detections WHERE "
            "ts_utc >= datetime('now','localtime','-7 days');"
        )
        week = cur.fetchone()[0]

        # all time (metadata)
        cur.execute("SELECT value FROM metadata WHERE key='total_count';")
        row = cur.fetchone()
        total_all_time = int(row[0]) if row else 0

    with state_lock:
        tm = test_mode
        today_test = test_count_today

    current = today_test if tm else today_db

    return jsonify(
        {
            "current_count": current,
            "yesterday_count": yesterday,
            "week_count": week,
            "total_all_time": total_all_time,
            "test_mode": tm,
        }
    )


@app.route("/api/stats/hourly", methods=["GET"])
def api_stats_hourly():
    """
    Returns hourly counts for today, filtered to only hours where
    the schedule is active (Enable + StartShow/ShowStop).
    """
    with db_lock:
        cur = db_conn.cursor()
        cur.execute(
            "SELECT strftime('%H', ts_utc,'localtime') AS h, "
            "COUNT(*) FROM detections "
            "WHERE date(ts_utc,'localtime') = date('now','localtime') "
            "GROUP BY h ORDER BY h;"
        )
        rows = cur.fetchall()

    now_local = datetime.now()
    hours = []
    counts = []

    for h_str, count in rows:
        try:
            h_int = int(h_str)
        except (TypeError, ValueError):
            continue

        dt_candidate = now_local.replace(hour=h_int, minute=0, second=0, microsecond=0)
        valid, active, enabled = compute_schedule_flags(dt_candidate)
        if not (valid and enabled and active):
            continue

        hours.append(h_str)
        counts.append(count)

    return jsonify({"hours": hours, "counts": counts})

# ------------- OLD COMPAT / MAINTENANCE -------------
@app.route("/reset_count", methods=["POST"])
def reset_count():
    """
    Kept for backward compatibility; resets DB total_count and test counter.
    """
    reset_total_count()
    with state_lock:
        globals()["test_count_today"] = 0
    return jsonify({"total_count": 0})


@app.route("/wipe_all", methods=["POST"])
def wipe_all():
    wipe_database()
    with state_lock:
        globals()["test_count_today"] = 0
    return jsonify({"ok": True})


@app.route("/api/total", methods=["GET"])
def api_total():
    return jsonify({"total_count": get_total_count()})


@app.route("/api/recent", methods=["GET"])
def api_recent():
    limit = int(request.args.get("limit", 20))
    with db_lock:
        cur = db_conn.cursor()
        cur.execute(
            "SELECT id, ts_utc, distance_mm "
            "FROM detections ORDER BY id DESC LIMIT ?;",
            (limit,),
        )
        rows = cur.fetchall()
    out = [{"id": r[0], "ts_utc": r[1], "distance_mm": r[2]} for r in rows]
    return jsonify(out)

@app.route("/api/service/restart", methods=["POST"])
def restart_service():
    try:
        subprocess.Popen(
            [
                "/usr/bin/sudo",
                "-n",
                "/bin/systemctl",
                "restart",
                "--no-block",
                "ShowMonLidarCounter"
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

@app.route('/get_time')
def get_time():
    # Returns the current system time to the webpage
    return jsonify({'time': datetime.now().strftime("%Y-%m-%d %H:%M:%S")})

@app.route('/sync_time', methods=['POST'])
def sync_time():
    try:
        # 1. Stop the background time service to unlock the port
        subprocess.run(['sudo', 'systemctl', 'stop', 'systemd-timesyncd'], check=True)
        
        # 2. Force the sync with Google
        subprocess.run(['sudo', 'ntpsec-ntpdate', '-u', 'time.google.com'], check=True)
        
        # 3. Start the background service again
        subprocess.run(['sudo', 'systemctl', 'start', 'systemd-timesyncd'], check=True)
        
        return jsonify({'status': 'success', 'message': 'Time synchronized successfully!'})
    except subprocess.CalledProcessError as e:
        # If it fails, try to ensure the service is restarted anyway
        subprocess.run(['sudo', 'systemctl', 'start', 'systemd-timesyncd'])
        return jsonify({'status': 'error', 'message': f'Sync failed: {str(e)}'}), 500

@app.route('/run_update', methods=['POST'])
def run_update():
    try:
        with open('config.json', 'r') as f:
            config = json.load(f)
        
        # Pull from the new separate section
        update_cfg = config.get('system_update', {})
        repo_url = update_cfg.get('repo_url')
        branch = update_cfg.get('branch', 'main')

        if not repo_url:
            return jsonify({'status': 'error', 'message': 'repo_url not found in system_update section'}), 400

        os.chdir('/home/admin/ShowMonLidarCounter')
        
        # 1. Update the Remote URL to match config
        subprocess.run(['git', 'remote', 'set-url', 'origin', repo_url], check=True)
        
        # 2. Fetch the latest code
        subprocess.run(['git', 'fetch', '--all'], check=True)
        
        # 3. Force the local code to match the public GitHub branch exactly
        subprocess.run(['git', 'reset', '--hard', f'origin/{branch}'], check=True)
        
        # 4. Restart Services (Using confirmed names)
        subprocess.run(['sudo', 'systemctl', 'restart', 'ShowMonLidarCounter.service'], check=False)
        subprocess.run(['sudo', 'systemctl', 'restart', 'LidarCounter.service'], check=False)

        return jsonify({'status': 'success', 'message': f'System updated to latest {branch} branch!'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

# Route to download the raw .db file
@app.route('/download_db')
def download_db():
    # Based on your screenshot, the file is cars.db
    db_path = '/home/admin/ShowMonLidarCounter/cars.db' 
    
    if os.path.exists(db_path):
        return send_file(db_path, as_attachment=True)
    else:
        return "Database file (cars.db) not found.", 404

# Route to download data as a CSV for Excel
@app.route('/download_csv')
def download_csv():
    db_path = '/home/admin/ShowMonLidarCounter/cars.db'
    if not os.path.exists(db_path):
        return "Database not found.", 404
        
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    try:
        # 1. Automatically find the table name
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = cursor.fetchall()
        
        if not tables:
            return "Database is empty (no tables found).", 404
        
        # Use the first table found (e.g., 'counts' or 'detections')
        table_name = tables[0][0]
        
        # 2. Fetch the data
        cursor.execute(f"SELECT * FROM {table_name}")
        rows = cursor.fetchall()
        column_names = [description[0] for description in cursor.description]
        
        # 3. Create the CSV
        si = io.StringIO()
        cw = csv.writer(si)
        cw.writerow(column_names)
        cw.writerows(rows)
        
        output = make_response(si.getvalue())
        output.headers["Content-Disposition"] = f"attachment; filename={table_name}_export.csv"
        output.headers["Content-type"] = "text/csv"
        return output
        
    except Exception as e:
        return f"Error reading database: {e}", 500
    finally:
        conn.close()

@app.route('/clear_db', methods=['POST'])
def clear_db():
    db_path = '/home/admin/ShowMonLidarCounter/cars.db'
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # This finds all tables and clears them
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = cursor.fetchall()
        for table in tables:
            cursor.execute(f"DELETE FROM {table[0]}")
            
        conn.commit()
        return jsonify({'status': 'success', 'message': 'Database cleared successfully!'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500
    finally:
        conn.close()
    
# ----------------- MAIN -----------------
if __name__ == "__main__":
    # sensor thread
    t = threading.Thread(target=sensor_loop, daemon=True)
    t.start()

    # schedule refresh thread (pull from GitHub)
    s = threading.Thread(target=schedule_loop, daemon=True)
    s.start()

    # real-time schedule evaluator (local clock)
    e = threading.Thread(target=schedule_eval_loop, daemon=True)
    e.start()

    app.run(host=HTTP_HOST, port=HTTP_PORT, threaded=True)
