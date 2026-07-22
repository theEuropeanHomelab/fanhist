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
    "  d=$(dirname \"$f\"); "
    "  grep -qx drivetemp \"$f\" 2>/dev/null || continue; "
    "  dev=$(ls \"$d\"/device/block 2>/dev/null | head -n1); "
    "  [ -z \"$dev\" ] && dev=$(basename \"$d\"); "
    "  echo \"$dev:$(cat \"$d\"/temp1_input)\"; "
    "done"
)

SETTINGS_DEFAULTS = {
    "idrac_host": os.environ.get("IDRAC_HOST", ""),
    "idrac_user": os.environ.get("IDRAC_USER", "root"),
    "idrac_pass": os.environ.get("IDRAC_PASS", ""),
    "cpu_sensor_names": [s.strip() for s in os.environ.get("CPU_SENSOR_NAME", "").split(",") if s.strip()],
    "cpu_temp_aggregation": os.environ.get("CPU_TEMP_AGGREGATION", "max"),
    "disk_ssh_host": os.environ.get("DISK_SSH_HOST", ""),
    "disk_ssh_user": os.environ.get("DISK_SSH_USER", "root"),
    "disk_temp_cmd": os.environ.get("DISK_TEMP_CMD", DEFAULT_DISK_TEMP_CMD),
    "disk_temp_aggregation": os.environ.get("DISK_TEMP_AGGREGATION", "avg"),
    "disk_names": [],  # empty = use every disk the command returns (backward compatible)
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
        "ipmitool", "-I", "lanplus", "-L", "ADMINISTRATOR",
        "-H", settings["idrac_host"], "-U", settings["idrac_user"], "-P", settings["idrac_pass"],
    ]


def _run(cmd, timeout, check=False):
    """subprocess.run wrapper that never lets the raw argv (which may contain
    the iDRAC password via -P) leak into an exception's string form — both
    TimeoutExpired and CalledProcessError include the full command by default."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=check)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"command timed out after {timeout}s")
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"command failed: {(exc.stderr or '').strip()}")


def ipmi_read_cpu_temps(settings):
    """Read one or more named sensors via ipmitool. Raises on failure/timeout."""
    names = settings["cpu_sensor_names"]
    if not names:
        return []
    cmd = _ipmi_base_cmd(settings) + ["sensor", "reading"] + names
    out = _run(cmd, settings["ipmi_timeout"])
    if out.returncode != 0:
        raise RuntimeError(f"ipmitool sensor reading failed: {out.stderr.strip()}")
    temps = []
    for line in out.stdout.splitlines():
        match = re.search(r"[-+]?\d+(\.\d+)?", line)
        if match:
            temps.append(float(match.group()))
    if not temps:
        raise RuntimeError(f"Could not parse sensor output: {out.stdout!r}")
    return temps


# IPMI Entity IDs we're confident about translating to a human label. Full
# range is defined by the IPMI spec (Table 43-13); only the ones relevant to
# a server chassis and that we're sure of are listed — anything else just
# shows the raw "entity" code so nothing is silently mislabeled.
KNOWN_ENTITY_IDS = {
    3: "Processor",
    7: "System Board",
    10: "Power Supply",
    29: "Fan/Cooling",
    55: "Air Inlet",
}


def ipmi_list_sensor_entities(settings):
    """Best-effort: map sensor name -> ordered list of raw Entity IDs (e.g. "7.2")
    via `ipmitool sdr elist full`. This is the same underlying IPMI metadata
    tools like Dell OMSA use to tell sensors apart by physical role even when
    their names collide — `sensor list` doesn't expose it, `sdr elist` does."""
    cmd = _ipmi_base_cmd(settings) + ["sdr", "elist", "full"]
    try:
        out = _run(cmd, settings["ipmi_timeout"])
    except RuntimeError:
        return {}
    if out.returncode != 0:
        return {}
    entities = {}
    for line in out.stdout.splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 4:
            continue
        name, entity = parts[0], parts[3]
        entities.setdefault(name, []).append(entity)
    return entities


def ipmi_list_sensors(settings):
    """List temperature sensors via `ipmitool sensor list`, for the Settings UI dropdown."""
    cmd = _ipmi_base_cmd(settings) + ["sensor", "list"]
    out = _run(cmd, settings["ipmi_timeout"])
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
        upper_crit = None
        if len(parts) > 8:
            try:
                upper_crit = float(parts[8])
            except ValueError:
                pass
        sensors.append({"name": name, "reading": reading, "unit": unit, "upper_crit": upper_crit})

    # Some boards (e.g. older Dell platforms) expose multiple sensors under the
    # exact same name (often just "Temp") with no more specific label available.
    # Number them so the UI can tell them apart even though ipmitool can't.
    name_counts = {}
    for s in sensors:
        name_counts[s["name"]] = name_counts.get(s["name"], 0) + 1
    seen = {}
    for s in sensors:
        if name_counts[s["name"]] > 1:
            seen[s["name"]] = seen.get(s["name"], 0) + 1
            s["instance"] = seen[s["name"]]
        else:
            s["instance"] = None

    # Layer in Entity IDs (best-effort — if this call fails or the output
    # doesn't parse as expected, sensors just keep their name-based instance
    # numbering above instead of the extra entity hint).
    entities = ipmi_list_sensor_entities(settings)
    name_index = {}
    for s in sensors:
        idx = name_index.get(s["name"], 0)
        name_index[s["name"]] = idx + 1
        ent_list = entities.get(s["name"], [])
        entity = ent_list[idx] if idx < len(ent_list) else None
        s["entity"] = entity
        entity_id = None
        if entity:
            try:
                entity_id = int(entity.split(".")[0])
            except ValueError:
                pass
        s["entity_label"] = KNOWN_ENTITY_IDS.get(entity_id)

    return sensors


def ipmi_set_manual_mode(settings):
    cmd = _ipmi_base_cmd(settings) + ["raw", "0x30", "0x30", "0x01", "0x00"]
    _run(cmd, settings["ipmi_timeout"], check=True)


def ipmi_set_fan_percent(settings, percent):
    percent = max(0, min(100, int(round(percent))))
    hex_val = f"0x{percent:02x}"
    cmd = _ipmi_base_cmd(settings) + ["raw", "0x30", "0x30", "0x02", "0xff", hex_val]
    _run(cmd, settings["ipmi_timeout"], check=True)


def read_disk_temps(settings):
    """Read one or more disk temperatures over SSH. Returns a list of
    {"name": str, "reading": float} dicts (empty list if not configured).

    Each output line is expected as "name:temp" (the default command emits
    the block device name, e.g. "sda:32000"), but a bare number per line
    (the old default command's format, for anyone with a saved custom
    command from before disk selection existed) is also accepted — those
    just get positional names like "disk1" instead of a real device name."""
    if not settings["disk_ssh_host"]:
        return []
    if not os.path.exists(SSH_KEY_PATH):
        raise RuntimeError("No SSH key yet — generate one in Settings and add it to your NAS")

    cmd = [
        "ssh", "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=no",
        "-i", SSH_KEY_PATH, f"{settings['disk_ssh_user']}@{settings['disk_ssh_host']}",
        settings["disk_temp_cmd"],
    ]
    out = _run(cmd, settings["ipmi_timeout"])
    if out.returncode != 0 or not out.stdout.strip():
        raise RuntimeError(f"disk temp SSH command failed: {out.stderr.strip()}")

    disks = []
    for line in out.stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        if ":" in line:
            name, _, value = line.partition(":")
            name = name.strip() or f"disk{len(disks) + 1}"
            value = value.strip()
        else:
            name = f"disk{len(disks) + 1}"
            value = line
        try:
            raw = float(value)
        except ValueError:
            continue
        # drivetemp/hwmon reports millidegrees; normalize to Celsius if needed
        reading = raw / 1000.0 if raw > 200 else raw
        disks.append({"name": name, "reading": reading})

    if not disks:
        raise RuntimeError(f"no parsable disk temps in output: {out.stdout!r}")
    return disks


def aggregate_temps(temps, mode):
    if not temps:
        return None
    if mode == "max":
        return max(temps)
    if mode == "min":
        return min(temps)
    return sum(temps) / len(temps)  # avg (default)


def selected_disk_readings(disks, selected_names):
    """Readings for the selected disks, or every disk if none are selected
    (preserves old behavior for anyone upgrading with disk temps already set up)."""
    if selected_names:
        disks = [d for d in disks if d["name"] in selected_names]
    return [d["reading"] for d in disks]


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

        cpu_temp = None
        disk_temps = []
        disk_temp = None
        error = None
        if settings["cpu_sensor_names"]:
            try:
                cpu_temps = ipmi_read_cpu_temps(settings)
                cpu_temp = aggregate_temps(cpu_temps, settings["cpu_temp_aggregation"])
            except Exception as exc:
                error = f"CPU temp read failed: {exc}"
        else:
            error = "No CPU sensors selected yet — pick some in Settings"

        try:
            all_disks = read_disk_temps(settings)
            disk_temps = selected_disk_readings(all_disks, settings["disk_names"])
            disk_temp = aggregate_temps(disk_temps, settings["disk_temp_aggregation"])
        except Exception as exc:
            error = (error + " | " if error else "") + f"Disk temp read failed: {exc}"

        candidates = [t for t in (cpu_temp, disk_temp) if t is not None]
        effective_temp = max(candidates) if candidates else None

        fan_percent = None
        if effective_temp is not None:
            curve = db_get_curve()
            fan_percent = curve_percent_for_temp(curve, effective_temp)
            try:
                # Re-assert manual mode every cycle, not just once. If anything
                # external (another tool, a BMC reset, iDRAC watchdog) flips
                # the fan controller back to automatic, this recovers on the
                # next cycle instead of silently sending ignored commands.
                ipmi_set_manual_mode(settings)
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
    return render_template("dashboard.html", active_page="dashboard")


@app.route("/settings")
def settings_page():
    return render_template("settings.html", active_page="settings")


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
        disks = read_disk_temps(settings)
        formatted = ", ".join(f"{d['name']}: {d['reading']:.1f}°C" for d in disks)
        return jsonify({"ok": True, "message": f"{len(disks)} disk(s): {formatted}", "disks": disks})
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)})


if __name__ == "__main__":
    db_init()
    threading.Thread(target=control_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=8081)
