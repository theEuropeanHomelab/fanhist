"""
fanhist - a small, self-hosted iDRAC fan controller with history.

Inspired by Hush (natankeddem/hush), but purpose-built and simpler:
- Reads CPU/Inlet temperature directly via ipmitool (fast, no Redfish/TLS involved)
- Optionally reads a disk temperature over SSH (e.g. TrueNAS drivetemp/hwmon)
- Applies a user-configurable temperature -> fan% curve via IPMI raw commands
- Logs every reading to SQLite and shows history on a small dashboard
- Everything (iDRAC creds, disk SSH host, SSH key) is configured from the web UI
"""

import json
import os
import re
import sqlite3
import subprocess
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta

from flask import Flask, jsonify, render_template, request

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
# DB_PATH is the only thing still set via env var (it's a deployment/storage
# concern, not something you'd want to change from the UI). Everything else
# lives in the "settings" row in SQLite and is edited from the dashboard.
# Env vars below are only used to *seed* that row the very first time the
# app runs, so upgraders with an existing docker-compose.yml keep working.

DB_PATH = os.environ.get("DB_PATH", "/data/fanhist.db")
GIT_SHA = os.environ.get("GIT_SHA", "unknown")

SSH_KEY_DIR = os.path.join(os.path.dirname(DB_PATH), "ssh")
SSH_KEY_PATH = os.path.join(SSH_KEY_DIR, "id_ed25519")

DEFAULT_DISK_TEMP_CMD = (
    "for f in /sys/class/hwmon/hwmon*/name; do "
    "  grep -qx drivetemp \"$f\" 2>/dev/null && cat \"$(dirname \"$f\")\"/temp1_input; "
    "done"
)

SETTINGS_DEFAULTS = {
    "idrac_host": os.environ.get("IDRAC_HOST", ""),
    "idrac_user": os.environ.get("IDRAC_USER", "root"),
    "idrac_pass": os.environ.get("IDRAC_PASS", ""),
    "cpu_sensor_name": os.environ.get("CPU_SENSOR_NAME", ""),
    "disk_ssh_host": os.environ.get("DISK_SSH_HOST", ""),
    "disk_ssh_user": os.environ.get("DISK_SSH_USER", "root"),
    "disk_temp_cmd": os.environ.get("DISK_TEMP_CMD", DEFAULT_DISK_TEMP_CMD),
    "disk_temp_aggregation": os.environ.get("DISK_TEMP_AGGREGATION", "avg"),
    "interval_seconds": int(os.environ.get("INTERVAL_SECONDS", "30")),
    "ipmi_timeout": int(os.environ.get("IPMI_TIMEOUT", "10")),
    "history_retention_days": int(os.environ.get("HISTORY_RETENTION_DAYS", "30")),
}

SETTINGS_FIELD_TYPES = {
    "interval_seconds": int,
    "ipmi_timeout": int,
    "history_retention_days": int,
}

SECRET_FIELDS = {"idrac_pass"}

DEFAULT_CURVE = [
    {"temp": 35, "percent": 5},
    {"temp": 45, "percent": 20},
    {"temp": 55, "percent": 40},
    {"temp": 65, "percent": 70},
    {"temp": 75, "percent": 100},
]

app = Flask(__name__)
_lock = threading.Lock()
_state = {
    "cpu_temp": None,
    "disk_temp": None,
    "disk_temps": [],
    "disk_count": 0,
    "effective_temp": None,
    "fan_percent": None,
    "last_update": None,
    "last_error": None,
}


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

def db_init():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS readings (
                ts TEXT NOT NULL,
                cpu_temp REAL,
                disk_temp REAL,
                effective_temp REAL,
                fan_percent REAL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT
            )"""
        )
        conn.commit()


def db_log_reading(cpu_temp, disk_temp, effective_temp, fan_percent):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "INSERT INTO readings (ts, cpu_temp, disk_temp, effective_temp, fan_percent) "
            "VALUES (?, ?, ?, ?, ?)",
            (datetime.utcnow().isoformat(), cpu_temp, disk_temp, effective_temp, fan_percent),
        )
        conn.commit()


def db_prune_old(retention_days):
    cutoff = (datetime.utcnow() - timedelta(days=retention_days)).isoformat()
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("DELETE FROM readings WHERE ts < ?", (cutoff,))
        conn.commit()


def db_get_history(hours):
    cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    with closing(sqlite3.connect(DB_PATH)) as conn:
        rows = conn.execute(
            "SELECT ts, cpu_temp, disk_temp, effective_temp, fan_percent "
            "FROM readings WHERE ts >= ? ORDER BY ts ASC",
            (cutoff,),
        ).fetchall()
    return [
        {
            "ts": r[0],
            "cpu_temp": r[1],
            "disk_temp": r[2],
            "effective_temp": r[3],
            "fan_percent": r[4],
        }
        for r in rows
    ]


def db_get_curve():
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute("SELECT value FROM config WHERE key = 'curve'").fetchone()
    if row:
        return json.loads(row[0])
    return DEFAULT_CURVE


def db_set_curve(curve):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "INSERT INTO config (key, value) VALUES ('curve', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (json.dumps(curve),),
        )
        conn.commit()


def db_get_settings():
    with closing(sqlite3.connect(DB_PATH)) as conn:
        row = conn.execute("SELECT value FROM config WHERE key = 'settings'").fetchone()
    settings = dict(SETTINGS_DEFAULTS)
    if row:
        settings.update(json.loads(row[0]))
    return settings


def db_set_settings(partial):
    settings = db_get_settings()
    settings.update(partial)
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "INSERT INTO config (key, value) VALUES ('settings', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (json.dumps(settings),),
        )
        conn.commit()
    return settings


def settings_public(settings):
    """Settings dict safe to send to the browser: secrets are replaced with a
    sentinel so the UI can show 'configured' without ever echoing the value back."""
    out = dict(settings)
    for key in SECRET_FIELDS:
        out[key] = "__SET__" if out.get(key) else ""
    return out


def settings_with_overrides(overrides):
    """Saved settings with unsaved form values layered on top, for the Test/List
    buttons — so they check what's currently typed, not just what was last saved."""
    settings = db_get_settings()
    for key, value in (overrides or {}).items():
        if key not in SETTINGS_DEFAULTS:
            continue
        if key in SECRET_FIELDS and value in ("", None, "__SET__"):
            continue
        if key in SETTINGS_FIELD_TYPES:
            try:
                value = SETTINGS_FIELD_TYPES[key](value)
            except (TypeError, ValueError):
                continue
        settings[key] = value
    return settings


# --------------------------------------------------------------------------
# IPMI helpers
# --------------------------------------------------------------------------

def _ipmi_base_cmd(settings):
    return [
        "ipmitool", "-I", "lanplus",
        "-H", settings["idrac_host"], "-U", settings["idrac_user"], "-P", settings["idrac_pass"],
    ]


def ipmi_read_cpu_temp(settings):
    """Read a named sensor via ipmitool. Raises on failure/timeout."""
    cmd = _ipmi_base_cmd(settings) + ["sensor", "reading", settings["cpu_sensor_name"]]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=settings["ipmi_timeout"])
    if out.returncode != 0:
        raise RuntimeError(f"ipmitool sensor reading failed: {out.stderr.strip()}")
    match = re.search(r"[-+]?\d+(\.\d+)?", out.stdout)
    if not match:
        raise RuntimeError(f"Could not parse sensor output: {out.stdout!r}")
    return float(match.group())


def ipmi_list_sensors(settings):
    """List temperature sensors via `ipmitool sensor list`, for the Settings UI dropdown."""
    cmd = _ipmi_base_cmd(settings) + ["sensor", "list"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=settings["ipmi_timeout"])
    if out.returncode != 0:
        raise RuntimeError(f"ipmitool sensor list failed: {out.stderr.strip()}")

    sensors = []
    for line in out.stdout.splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            continue
        name, reading_str, unit = parts[0], parts[1], parts[2]
        if not name or "degree" not in unit.lower():
            continue
        try:
            reading = float(reading_str)
        except ValueError:
            reading = None
        sensors.append({"name": name, "reading": reading, "unit": unit})
    return sensors


def ipmi_set_manual_mode(settings):
    cmd = _ipmi_base_cmd(settings) + ["raw", "0x30", "0x30", "0x01", "0x00"]
    subprocess.run(cmd, capture_output=True, text=True, timeout=settings["ipmi_timeout"], check=True)


def ipmi_set_fan_percent(settings, percent):
    percent = max(0, min(100, int(round(percent))))
    hex_val = f"0x{percent:02x}"
    cmd = _ipmi_base_cmd(settings) + ["raw", "0x30", "0x30", "0x02", "0xff", hex_val]
    subprocess.run(cmd, capture_output=True, text=True, timeout=settings["ipmi_timeout"], check=True)


def read_disk_temps(settings):
    """Read one or more disk temperatures over SSH. Returns a list of °C values
    (empty list if not configured)."""
    if not settings["disk_ssh_host"]:
        return []
    if not os.path.exists(SSH_KEY_PATH):
        raise RuntimeError("No SSH key yet — generate one in Settings and add it to your NAS")

    cmd = [
        "ssh", "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=no",
        "-i", SSH_KEY_PATH, f"{settings['disk_ssh_user']}@{settings['disk_ssh_host']}",
        settings["disk_temp_cmd"],
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=settings["ipmi_timeout"])
    if out.returncode != 0 or not out.stdout.strip():
        raise RuntimeError(f"disk temp SSH command failed: {out.stderr.strip()}")

    temps = []
    for line in out.stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = float(line)
        except ValueError:
            continue
        # drivetemp/hwmon reports millidegrees; normalize to Celsius if needed
        temps.append(raw / 1000.0 if raw > 200 else raw)

    if not temps:
        raise RuntimeError(f"no parsable disk temps in output: {out.stdout!r}")
    return temps


def aggregate_disk_temp(temps, settings):
    if not temps:
        return None
    aggregation = settings["disk_temp_aggregation"]
    if aggregation == "max":
        return max(temps)
    if aggregation == "min":
        return min(temps)
    return sum(temps) / len(temps)  # avg (default)


# --------------------------------------------------------------------------
# SSH key management (for disk temp reads)
# --------------------------------------------------------------------------

def ssh_key_public_text():
    pub_path = SSH_KEY_PATH + ".pub"
    if not os.path.exists(pub_path):
        return None
    with open(pub_path) as f:
        return f.read().strip()


def ssh_key_generate():
    os.makedirs(SSH_KEY_DIR, exist_ok=True)
    for path in (SSH_KEY_PATH, SSH_KEY_PATH + ".pub"):
        if os.path.exists(path):
            os.remove(path)
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", SSH_KEY_PATH, "-N", "", "-C", "fanhist"],
        capture_output=True, text=True, timeout=10, check=True,
    )
    os.chmod(SSH_KEY_PATH, 0o600)
    return ssh_key_public_text()


# --------------------------------------------------------------------------
# Curve interpolation
# --------------------------------------------------------------------------

def curve_percent_for_temp(curve, temp):
    points = sorted(curve, key=lambda p: p["temp"])
    if temp <= points[0]["temp"]:
        return points[0]["percent"]
    if temp >= points[-1]["temp"]:
        return points[-1]["percent"]
    for a, b in zip(points, points[1:]):
        if a["temp"] <= temp <= b["temp"]:
            span = b["temp"] - a["temp"]
            if span == 0:
                return a["percent"]
            ratio = (temp - a["temp"]) / span
            return a["percent"] + ratio * (b["percent"] - a["percent"])
    return points[-1]["percent"]


# --------------------------------------------------------------------------
# Control loop
# --------------------------------------------------------------------------

def control_loop():
    manual_mode_key = None  # (host, user, pass) that manual mode was last set for

    while True:
        settings = db_get_settings()
        interval = settings["interval_seconds"] or 30

        if not settings["idrac_host"] or not settings["idrac_pass"]:
            with _lock:
                _state.update(
                    last_error="iDRAC not configured yet — fill in Settings",
                    last_update=datetime.utcnow().isoformat(),
                )
            time.sleep(interval)
            continue

        idrac_key = (settings["idrac_host"], settings["idrac_user"], settings["idrac_pass"])
        if idrac_key != manual_mode_key:
            try:
                ipmi_set_manual_mode(settings)
                manual_mode_key = idrac_key
            except Exception as exc:
                with _lock:
                    _state.update(
                        last_error=f"Failed to set manual fan mode: {exc}",
                        last_update=datetime.utcnow().isoformat(),
                    )
                time.sleep(interval)
                continue

        cpu_temp = None
        disk_temps = []
        disk_temp = None
        error = None
        if settings["cpu_sensor_name"]:
            try:
                cpu_temp = ipmi_read_cpu_temp(settings)
            except Exception as exc:
                error = f"CPU temp read failed: {exc}"
        else:
            error = "CPU sensor not selected yet — pick one in Settings"

        try:
            disk_temps = read_disk_temps(settings)
            disk_temp = aggregate_disk_temp(disk_temps, settings)
        except Exception as exc:
            error = (error + " | " if error else "") + f"Disk temp read failed: {exc}"

        candidates = [t for t in (cpu_temp, disk_temp) if t is not None]
        effective_temp = max(candidates) if candidates else None

        fan_percent = None
        if effective_temp is not None:
            curve = db_get_curve()
            fan_percent = curve_percent_for_temp(curve, effective_temp)
            try:
                ipmi_set_fan_percent(settings, fan_percent)
            except Exception as exc:
                error = (error + " | " if error else "") + f"Fan set failed: {exc}"

        with _lock:
            _state.update(
                cpu_temp=cpu_temp,
                disk_temp=disk_temp,
                disk_temps=disk_temps,
                disk_count=len(disk_temps),
                effective_temp=effective_temp,
                fan_percent=fan_percent,
                last_update=datetime.utcnow().isoformat(),
                last_error=error,
            )

        if effective_temp is not None:
            db_log_reading(cpu_temp, disk_temp, effective_temp, fan_percent)
        db_prune_old(settings["history_retention_days"])

        time.sleep(interval)


# --------------------------------------------------------------------------
# Web routes
# --------------------------------------------------------------------------

@app.route("/")
def dashboard():
    return render_template("dashboard.html")


@app.route("/api/status")
def api_status():
    with _lock:
        return jsonify(_state)


@app.route("/api/version")
def api_version():
    return jsonify({"git_sha": GIT_SHA})


@app.route("/api/history")
def api_history():
    hours = int(request.args.get("hours", 24))
    return jsonify(db_get_history(hours))


@app.route("/api/curve", methods=["GET", "POST"])
def api_curve():
    if request.method == "POST":
        curve = request.get_json()
        if not isinstance(curve, list) or not curve:
            return jsonify({"error": "curve must be a non-empty list"}), 400
        db_set_curve(curve)
        return jsonify({"ok": True})
    return jsonify(db_get_curve())


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "POST":
        body = request.get_json()
        if not isinstance(body, dict):
            return jsonify({"error": "settings must be an object"}), 400

        partial = {}
        for key, value in body.items():
            if key not in SETTINGS_DEFAULTS:
                continue
            if key in SECRET_FIELDS and value in ("", None, "__SET__"):
                continue  # blank/unset means "leave existing secret untouched"
            if key in SETTINGS_FIELD_TYPES:
                try:
                    value = SETTINGS_FIELD_TYPES[key](value)
                except (TypeError, ValueError):
                    return jsonify({"error": f"invalid value for {key}"}), 400
            partial[key] = value

        settings = db_set_settings(partial)
        return jsonify(settings_public(settings))

    return jsonify(settings_public(db_get_settings()))


@app.route("/api/ssh-key", methods=["GET"])
def api_ssh_key():
    pub = ssh_key_public_text()
    return jsonify({"exists": pub is not None, "public_key": pub})


@app.route("/api/ssh-key/generate", methods=["POST"])
def api_ssh_key_generate():
    try:
        pub = ssh_key_generate()
    except subprocess.CalledProcessError as exc:
        return jsonify({"error": f"key generation failed: {exc.stderr}"}), 500
    return jsonify({"public_key": pub})


@app.route("/api/sensors/idrac", methods=["POST"])
def api_sensors_idrac():
    settings = settings_with_overrides(request.get_json(silent=True))
    if not settings["idrac_host"] or not settings["idrac_pass"]:
        return jsonify({"ok": False, "message": "Fill in iDRAC host and password first"})
    try:
        sensors = ipmi_list_sensors(settings)
        if not sensors:
            return jsonify({"ok": False, "message": "No temperature sensors found"})
        return jsonify({"ok": True, "sensors": sensors})
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)})


@app.route("/api/test/disk", methods=["POST"])
def api_test_disk():
    settings = settings_with_overrides(request.get_json(silent=True))
    if not settings["disk_ssh_host"]:
        return jsonify({"ok": False, "message": "Fill in disk SSH host first"})
    try:
        temps = read_disk_temps(settings)
        formatted = ", ".join(f"{t:.1f}°C" for t in temps)
        return jsonify({"ok": True, "message": f"{len(temps)} disk(s): {formatted}"})
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)})


if __name__ == "__main__":
    db_init()
    threading.Thread(target=control_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=8081)
