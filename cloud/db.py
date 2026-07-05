"""
db.py  —  Database helpers + ingestion storage + sample closing logic.
Complete final version (4 triggers + validity gate + sweep).
"""

import sqlite3
from datetime import datetime, timedelta

DB_FILE = "flocdash.db"

TEST_DURATION_MIN = 100
MIN_VALID_MIN     = 30

current_sample = {}


def get_connection():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row
    return conn


def get_or_create_device(conn, plant_id, plc_id):
    cur = conn.cursor()
    cur.execute("SELECT id FROM devices WHERE plant_id = ? AND plc_id = ?",
                (plant_id, plc_id))
    row = cur.fetchone()
    if row:
        return row["id"]
    cur.execute("INSERT INTO devices (plant_id, plc_id) VALUES (?, ?)",
                (plant_id, plc_id))
    conn.commit()
    return cur.lastrowid


def update_last_seen(conn, device_id, ts):
    conn.execute("UPDATE devices SET last_seen = ? WHERE id = ?", (ts, device_id))
    conn.commit()


def find_sludge_value(payload):
    for key, value in payload.items():
        if key.startswith("SVOL_1:"):
            try:
                return float(value)
            except (ValueError, TypeError):
                return None
    return None


def store_payload(conn, payload):
    plant_id = payload.get("plantId")
    plc_id = payload.get("plcId")
    if not plant_id or not plc_id:
        print("Skipping payload with no plantId/plcId")
        return

    device_id = get_or_create_device(conn, plant_id, plc_id)
    now = datetime.utcnow().isoformat()
    update_last_seen(conn, device_id, now)

    if "error_code" in payload:
        conn.execute(
            """INSERT INTO device_errors (device_id, ts, error_code, message, sample_id)
               VALUES (?, ?, ?, ?, ?)""",
            (device_id, now, payload.get("error_code"), payload.get("message"),
             payload.get("sampleId")))
        conn.commit()
        print(f"Stored error {payload.get('error_code')} for device {device_id}")
        return

    sludge_value = find_sludge_value(payload)
    sample_id = payload.get("sampleId")
    minute = payload.get("minute")
    state = payload.get("state")
    floating = 1 if state == "floatingSludge" else 0

    if sample_id:
        prev = current_sample.get(device_id)
        if prev and prev != sample_id:
            print(f"New sample {sample_id} on device {device_id}; closing previous {prev}.")
            close_sample(conn, device_id, prev)
        current_sample[device_id] = sample_id

    conn.execute(
        """INSERT OR IGNORE INTO readings
           (device_id, ts, sample_id, minute, state, sludge_value, floating)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (device_id, now, sample_id, minute, state, sludge_value, floating))
    conn.commit()
    print(f"Stored reading: device={device_id} state={state} "
          f"minute={minute} sludge={sludge_value}")

    if state == "end" and sample_id:
        close_sample(conn, device_id, sample_id)
        current_sample.pop(device_id, None)
    elif sample_id and minute is not None and minute >= TEST_DURATION_MIN:
        close_sample(conn, device_id, sample_id)
        current_sample.pop(device_id, None)


def _value_at_minute(readings, target_minute, tolerance=2):
    best, best_diff = None, tolerance + 1
    for r in readings:
        if r["minute"] is None or r["sludge_value"] is None:
            continue
        diff = abs(r["minute"] - target_minute)
        if diff <= tolerance and diff < best_diff:
            best, best_diff = r["sludge_value"], diff
    return best


def _classify_settling(sv30, sv90, floating):
    if floating:
        return "rising"
    if sv30 is None:
        return "unknown"
    if sv30 > 90:
        return "bulking"
    if sv90 is not None and (sv30 - sv90) < 2 and sv30 > 50:
        return "slow"
    return "healthy"


def _reached_30(readings):
    for r in readings:
        if r["minute"] is not None and r["minute"] >= MIN_VALID_MIN:
            return True
    return False


def close_sample(conn, device_id, sample_id):
    cur = conn.cursor()
    cur.execute(
        """SELECT minute, sludge_value, floating, ts
           FROM readings WHERE device_id = ? AND sample_id = ?
           ORDER BY minute ASC""",
        (device_id, sample_id))
    readings = cur.fetchall()
    if not readings:
        return
    if not _reached_30(readings):
        print(f"Sample {sample_id} under 30 min — discarded (not shown).")
        return
    compute_sample_metrics(conn, device_id, sample_id, readings)


def compute_sample_metrics(conn, device_id, sample_id, readings):
    started_at = readings[0]["ts"]
    ended_at   = readings[-1]["ts"]

    sv5  = _value_at_minute(readings, 5, tolerance=3)
    sv30 = _value_at_minute(readings, 30)
    sv60 = _value_at_minute(readings, 60)
    sv90 = _value_at_minute(readings, 90)

    initial_velocity = None
    if sv5 is not None and sv30 is not None:
        initial_velocity = round((sv5 - sv30) / 25.0, 3)

    compaction_ratio = None
    if sv30 is not None and sv90 not in (None, 0):
        compaction_ratio = round(sv30 / sv90, 3)

    floating_detected = 0
    floating_first_minute = None
    floating_first_at = None
    for r in readings:
        if r["floating"] == 1:
            floating_detected = 1
            floating_first_minute = r["minute"]
            floating_first_at = r["ts"]
            break

    settling_class = _classify_settling(sv30, sv90, floating_detected)

    conn.execute(
        """INSERT INTO samples
             (device_id, sample_id, started_at, ended_at, status,
              sv5, sv30, sv60, sv90, initial_velocity, compaction_ratio,
              settling_class, floating_detected, floating_first_minute, floating_first_at)
           VALUES (?, ?, ?, ?, 'complete', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(device_id, sample_id) DO UPDATE SET
             ended_at=excluded.ended_at, status='complete',
             sv5=excluded.sv5, sv30=excluded.sv30, sv60=excluded.sv60, sv90=excluded.sv90,
             initial_velocity=excluded.initial_velocity,
             compaction_ratio=excluded.compaction_ratio,
             settling_class=excluded.settling_class,
             floating_detected=excluded.floating_detected,
             floating_first_minute=excluded.floating_first_minute,
             floating_first_at=excluded.floating_first_at""",
        (device_id, sample_id, started_at, ended_at,
         sv5, sv30, sv60, sv90, initial_velocity, compaction_ratio,
         settling_class, floating_detected, floating_first_minute, floating_first_at))
    conn.commit()
    print(f"Computed sample {sample_id}: sv30={sv30} sv90={sv90} "
          f"class={settling_class} floating={floating_detected}")


def sweep_stuck_samples(conn):
    cur = conn.cursor()
    cur.execute(
        """SELECT r.device_id AS device_id, r.sample_id AS sample_id,
                  MAX(r.minute) AS last_min, MAX(r.ts) AS last_ts
           FROM readings r
           WHERE r.sample_id IS NOT NULL
             AND NOT EXISTS (
                 SELECT 1 FROM samples s
                 WHERE s.device_id = r.device_id AND s.sample_id = r.sample_id
                   AND s.status = 'complete')
           GROUP BY r.device_id, r.sample_id""")
    rows = cur.fetchall()
    now = datetime.utcnow()

    for row in rows:
        last_min = row["last_min"] or 0
        try:
            last_ts = datetime.fromisoformat(row["last_ts"])
        except Exception:
            continue
        minutes_remaining = max(0, TEST_DURATION_MIN - last_min)
        would_end_at = last_ts + timedelta(minutes=minutes_remaining)
        if now >= would_end_at:
            print(f"Sweep closing stuck sample {row['sample_id']} (last min {last_min}).")
            close_sample(conn, row["device_id"], row["sample_id"])
