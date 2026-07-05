# FlocDetector Cloud Dashboard — Implementation Guide

A step-by-step guide to building an edge-to-cloud IoT monitoring system for
wastewater sludge-settling analysis. This document teaches **how** and **why**,
so you can rebuild it from scratch and understand every decision.

---

## Table of Contents

1. [What You're Building](#1-what-youre-building)
2. [Architecture Overview](#2-architecture-overview)
3. [Key Design Decisions (and Why)](#3-key-design-decisions-and-why)
4. [Prerequisites](#4-prerequisites)
5. [Part A — The Database](#part-a--the-database)
6. [Part B — The Ingestion Service](#part-b--the-ingestion-service)
7. [Part C — The Web Dashboard](#part-c--the-web-dashboard)
8. [Part D — The Image Service + S3](#part-d--the-image-service--s3)
9. [Part E — Node-RED Integration](#part-e--node-red-integration)
10. [Part F — Deployment with PM2](#part-f--deployment-with-pm2)
11. [Part G — Security](#part-g--security)
12. [Troubleshooting & Lessons Learned](#troubleshooting--lessons-learned)
13. [Future Hardening](#future-hardening)

---

## 1. What You're Building

A settling test (SV30) measures how activated sludge settles in a beaker over
time. A camera + ML model on a Raspberry Pi watches a beaker, detects the sludge
interface each minute, and reports the settled percentage. This system:

- **Ingests** that per-minute telemetry from many field units via MQTT
- **Stores** it in a time-series-friendly database
- **Computes** per-test metrics (SV30, SV60, SV90, settling class)
- **Displays** live settling curves in a web dashboard, with real-time updates
- **Captures** snapshot images at key moments (30/60/90 min, floating sludge, errors),
  stores them in S3, and shows them as clickable icons on the curve
- **Serves** multiple users with role-based access (admin sees all units;
  regular users see only their assigned units)

The end result looks like a fleet-monitoring dashboard: a grid of device cards,
and per-device pages with an interactive settling curve annotated with event tags.

---

## 2. Architecture Overview

```
  FIELD UNIT (Raspberry Pi)                      CLOUD (single EC2 instance)
  ┌────────────────────────┐                     ┌──────────────────────────────┐
  │  Camera → YOLO model    │                     │                              │
  │        │                │   MQTT (telemetry)  │   ingest.py  ──┐             │
  │        ├─ main.py ──────┼─────────────────────┼──► (subscribe) │             │
  │        │                │   via AWS IoT Core  │                ▼             │
  │        │                │                     │            flocdash.db       │
  │   Node-RED              │                     │            (SQLite)          │
  │        │                │   HTTP (image POST) │                ▲             │
  │        └────────────────┼─────────────────────┼──► image_service.py ──► S3   │
  │                         │   multipart/form    │                │             │
  └────────────────────────┘                     │            app.py (Flask)    │
                                                  │            dashboard + SSE   │
                              browser ◄───────────┼──── port 5000                │
                                                  └──────────────────────────────┘
```

**Three independent Python processes on the cloud box:**

| Process | Role | Port |
|---|---|---|
| `ingest.py` | Subscribes to MQTT, writes telemetry to the DB | — (outbound) |
| `app.py` | Flask dashboard; serves pages, live updates, presigned image URLs | 5000 |
| `image_service.py` | Receives image uploads, stores in S3, records DB rows | 5001 |

They share one SQLite database file. They are decoupled: ingestion can run
without the dashboard, images arrive independently of telemetry.

---

## 3. Key Design Decisions (and Why)

Understanding these is more valuable than the code itself.

### Why SQLite instead of PostgreSQL/TimescaleDB?

The cloud box has **1.9 GB RAM total** (~1.2 GB usable) and already runs other
production services. Postgres + TimescaleDB would consume hundreds of MB just
idling. SQLite is a file — zero server process, near-zero idle memory. At the
scale of a modest fleet with per-minute readings, SQLite handles the load easily.

**Lesson:** match the datastore to the hardware. A "better" database that
doesn't fit is worse than a "lesser" one that runs reliably.

### Why three separate Flask processes instead of one FastAPI service?

FastAPI + uvicorn is heavier (~150 MB) and its startup spike triggered the
kernel's out-of-memory killer on this box. Three plain-Flask processes (~30 MB
each = ~90 MB total) fit comfortably. We kept them **separate** for isolation:
ingestion crashing shouldn't take down the dashboard.

**Lesson:** "separate services" is an architecture goal; the *framework* is an
implementation detail you trade off against your constraints.

### Why subscribe directly to MQTT instead of using the existing SQS queue?

The production system uses an AWS IoT Rule → SQS pipeline. We chose to have the
dashboard subscribe **directly** to the MQTT topic instead. Reasons:
portability (no AWS-specific queue lock-in — this could point at any MQTT
broker), and simplicity. We used a **unique MQTT client ID** so our subscription
doesn't collide with production's.

**Lesson:** for a portable/generalizable product, avoid vendor-specific glue
where a standard protocol (MQTT) already gives you what you need.

### Why precompute sample metrics instead of calculating on read?

Each completed test gets a row in a `samples` table with SV30/60/90 and a
settling classification already computed. The dashboard reads these directly.
Computing on every page load would repeatedly scan the readings table.

**Lesson:** for data that's written once and read many times, compute at write
time.

### Why store only the S3 key in the database, not the image?

Images are large binaries. Databases handle them poorly (bloat, slow queries).
S3 is built for blob storage and cheap. We store the **key** (a short string
path) in the DB, and generate a temporary **presigned URL** on demand when a
user wants to view the image. The bucket stays private.

**Lesson:** databases for structured/relational data, object storage for blobs.
Link them by key.

---

## 4. Prerequisites

**On the cloud box (Ubuntu 24.04 assumed):**

```bash
# System packages
sudo apt update
sudo apt install -y python3-venv python3-pip sqlite3

# A dedicated project directory
mkdir -p ~/flocdashboard && cd ~/flocdashboard

# A virtual environment keeps dependencies isolated from the system Python
python3 -m venv venv
source venv/bin/activate

# Python dependencies
pip install flask paho-mqtt boto3 requests werkzeug

# PM2 for process management (needs Node.js)
sudo npm install -g pm2
```

**AWS resources you need:**
- An **S3 bucket** (e.g. `flocdetectorimage`) in your region (e.g. `ap-south-1`)
- An **IAM access key + secret** with `s3:PutObject`, `s3:GetObject`, `s3:ListBucket` on that bucket
- Access to your **AWS IoT Core** MQTT endpoint, plus device certificates
  (`AmazonRootCA1.pem`, a device cert, and a private key)

**A secrets file** — create `~/flocdashboard/.env.s3` and lock it down:

```bash
nano ~/flocdashboard/.env.s3
```
```
AWS_ACCESS_KEY_ID=your_key
AWS_SECRET_ACCESS_KEY=your_secret
AWS_REGION=ap-south-1
S3_BUCKET=flocdetectorimage
UPLOAD_TOKEN=generate_a_long_random_string
FLASK_SECRET_KEY=generate_another_long_random_string
```
```bash
chmod 600 ~/flocdashboard/.env.s3   # only you can read it
```

Generate the random tokens with:
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

**Why a secrets file:** credentials never live in code or version control.
`chmod 600` means only your user can read it. Every service loads from here.

---

## Part A — The Database

The database is the shared backbone. Build it first so the other services have
something to write to and read from.

### The schema — seven tables

Create a file `schema.sql`:

```sql
-- A physical field unit. Identified by the pair (plant_id, plc_id).
CREATE TABLE devices (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    plant_id    TEXT NOT NULL,
    plc_id      TEXT NOT NULL,
    name        TEXT,
    location    TEXT,
    first_seen  TEXT DEFAULT (datetime('now')),
    last_seen   TEXT,
    UNIQUE(plant_id, plc_id)      -- one row per physical device
);

-- One completed settling test. Metrics are PRECOMPUTED here at close time.
CREATE TABLE samples (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id            INTEGER NOT NULL REFERENCES devices(id),
    sample_id            TEXT NOT NULL,          -- the device's test UUID
    started_at           TEXT,
    ended_at             TEXT,
    status               TEXT DEFAULT 'running', -- 'running' | 'complete'
    sv5                  REAL,   -- settled % at minute 5
    sv30                 REAL,   -- at minute 30 (the key metric)
    sv60                 REAL,
    sv90                 REAL,
    initial_velocity     REAL,   -- how fast it settled early
    compaction_ratio     REAL,
    settling_class       TEXT,   -- 'healthy' | 'slow' | 'bulking' | 'rising'
    floating_detected    INTEGER DEFAULT 0,      -- 0/1 boolean
    floating_first_minute INTEGER,
    floating_first_at    TEXT,
    created_at           TEXT DEFAULT (datetime('now')),
    UNIQUE(device_id, sample_id)
);

-- Every per-minute reading. The raw time-series.
CREATE TABLE readings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id    INTEGER NOT NULL REFERENCES devices(id),
    ts           TEXT NOT NULL,
    sample_id    TEXT,                 -- which test this belongs to (null when idle)
    minute       INTEGER,              -- minute into the test
    state        TEXT,                 -- idle/start/ongoing/30Mark/.../error
    sludge_value REAL,                 -- normalized settled %
    floating     INTEGER DEFAULT 0,
    UNIQUE(device_id, sample_id, minute, state)   -- idempotency guard
);

-- Errors reported by a device. Carry sample_id when they occur mid-test.
CREATE TABLE device_errors (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id  INTEGER NOT NULL REFERENCES devices(id),
    ts         TEXT NOT NULL,
    error_code TEXT,
    message    TEXT,
    sample_id  TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Image references. The bytes live in S3; here we store only the key.
CREATE TABLE images (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id   INTEGER NOT NULL REFERENCES devices(id),
    sample_id   TEXT,
    state       TEXT,             -- 30Mark/60Mark/90Mark/floatingSludge/error
    s3_key      TEXT NOT NULL,    -- path within the bucket
    captured_at TEXT,
    created_at  TEXT DEFAULT (datetime('now'))
);

-- Dashboard users.
CREATE TABLE users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'user',   -- 'admin' | 'user'
    created_at    TEXT DEFAULT (datetime('now'))
);

-- Which non-admin users can see which devices.
CREATE TABLE user_devices (
    user_id   INTEGER NOT NULL REFERENCES users(id),
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, device_id)
);

-- Indexes for the common query paths.
CREATE INDEX idx_samples_device_time ON samples(device_id, started_at);
CREATE INDEX idx_readings_sample     ON readings(device_id, sample_id, minute);
CREATE INDEX idx_errors_device_time  ON device_errors(device_id, ts);
CREATE INDEX idx_images_sample       ON images(device_id, sample_id);
```

Create the database:

```bash
cd ~/flocdashboard
sqlite3 flocdash.db < schema.sql
sqlite3 flocdash.db ".tables"    # verify all 7 tables exist
```

### Why these design choices

- **`UNIQUE(plant_id, plc_id)`** — a device is identified by the *pair*, not one
  field. Two plants might reuse PLC ids.
- **`UNIQUE(device_id, sample_id, minute, state)` on readings** — this is your
  **idempotency guard**. If the same reading arrives twice (network retry,
  reconnect), the second insert is rejected instead of duplicating data. Critical
  for MQTT which can redeliver.
- **Precomputed metrics on `samples`** — SV30 etc. are calculated once when the
  test closes, not on every dashboard load.
- **`sample_id` nullable on readings** — when a device is idle, it still sends
  heartbeat readings with no active test.
- **TEXT for timestamps** — SQLite has no native datetime type; ISO-8601 strings
  sort correctly and are human-readable.


### The data-access layer — `db.py`

All database logic lives in one module so both `ingest.py` and `app.py` share it.
The core responsibilities:

1. **`get_connection()`** — open SQLite with the right settings
2. **`get_or_create_device()`** — register a device on first sighting
3. **`store_payload()`** — the main entry: take an MQTT payload, store it
4. **`compute_sample_metrics()` / `close_sample()`** — finalize a test
5. **`sweep_stuck_samples()`** — clean up tests that never got a proper close

```python
import sqlite3
from datetime import datetime, timedelta

DB_FILE = "/home/ubuntu/flocdashboard/flocdash.db"

TEST_DURATION_MIN = 100    # a test runs ~100 minutes
MIN_VALID_MIN     = 30     # a test must reach 30 min to be worth keeping

# In-memory tracking of the current sample per device (for close detection).
current_sample = {}


def get_connection():
    """Open SQLite tuned for our multi-threaded, foreign-key usage."""
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row            # rows behave like dicts
    conn.execute("PRAGMA foreign_keys = ON")  # enforce FK constraints
    return conn
```

**Why `check_same_thread=False`:** Flask serves requests on multiple threads;
by default SQLite forbids sharing a connection across threads. We allow it
(and keep writes short) so the dashboard's threads can share connections.

**Why `row_factory = sqlite3.Row`:** lets you write `row["sv30"]` instead of
`row[3]` — readable and robust to column reordering.

```python
def get_or_create_device(conn, plant_id, plc_id):
    """Return the device id for (plant_id, plc_id), creating the row if new."""
    row = conn.execute(
        "SELECT id FROM devices WHERE plant_id=? AND plc_id=?",
        (plant_id, plc_id)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO devices (plant_id, plc_id, first_seen, last_seen) "
        "VALUES (?, ?, datetime('now'), datetime('now'))",
        (plant_id, plc_id))
    conn.commit()
    return cur.lastrowid
```

This is why devices **auto-register** — the first time a unit sends telemetry,
a row appears. No manual provisioning of devices in the dashboard.

```python
def find_sludge_value(payload):
    """
    The settled % arrives under a key like 'SVOL_1:PAR_PAK' where the suffix
    varies per client (PAR_PAK, VAT_VAT, EMS_EMS...). We normalize by matching
    the stable prefix 'SVOL_1:'.
    """
    for key, val in payload.items():
        if key.startswith("SVOL_1:"):
            try:
                return float(val)
            except (ValueError, TypeError):
                return None
    return None
```

**This is a real-world data-normalization pattern:** the same logical field has
different key names per customer. Rather than hard-code every variant, match the
stable part of the key.

```python
def store_payload(payload):
    """Main ingestion entry point. Store one MQTT payload."""
    plant_id = payload.get("plantId")
    plc_id   = payload.get("plcId")
    if not plant_id or not plc_id:
        return   # can't attribute this reading to a device

    conn = get_connection()
    device_id = get_or_create_device(conn, plant_id, plc_id)
    now = datetime.utcnow().isoformat()
    conn.execute("UPDATE devices SET last_seen=? WHERE id=?", (now, device_id))

    # --- Error branch: errors carry sample_id when mid-test ---
    if "error_code" in payload:
        conn.execute(
            "INSERT INTO device_errors (device_id, ts, error_code, message, sample_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (device_id, now, payload.get("error_code"),
             payload.get("message"), payload.get("sampleId")))
        conn.commit()
        conn.close()
        return

    # --- Normal reading ---
    sample_id = payload.get("sampleId")
    minute    = payload.get("minute")
    state     = payload.get("state")
    sludge    = find_sludge_value(payload)
    floating  = 1 if state == "floatingSludge" else 0

    # NEW sample id for this device => the previous test is over; close it.
    prev = current_sample.get(device_id)
    if sample_id and prev and prev != sample_id:
        close_sample(conn, device_id, prev)
    if sample_id:
        current_sample[device_id] = sample_id

    # Insert the reading (idempotent via the UNIQUE constraint).
    try:
        conn.execute(
            "INSERT INTO readings (device_id, ts, sample_id, minute, state, sludge_value, floating) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (device_id, now, sample_id, minute, state, sludge, floating))
    except sqlite3.IntegrityError:
        pass   # duplicate reading — safely ignored

    # Closing triggers: an explicit end, or reaching the full duration.
    if state == "end" or (minute is not None and minute >= TEST_DURATION_MIN):
        if sample_id:
            close_sample(conn, device_id, sample_id)

    conn.commit()
    conn.close()
```


### Closing a sample and computing metrics

A test needs to be "closed" — marked complete with its metrics computed. There
are **four triggers** for closing, which together handle every real-world case:

1. **Explicit `end` state** — the device says the test is done
2. **Reached full duration** — a reading at minute ≥ 100
3. **A new sample id appears** — the previous test must be over
4. **The sweep** (below) — a safety net for tests that stopped sending

```python
def _value_at_minute(conn, device_id, sample_id, target, tolerance=4):
    """Get the sludge value near a target minute (e.g. 30 for SV30)."""
    row = conn.execute(
        "SELECT sludge_value FROM readings "
        "WHERE device_id=? AND sample_id=? AND minute BETWEEN ? AND ? "
        "AND sludge_value IS NOT NULL ORDER BY ABS(minute-?) LIMIT 1",
        (device_id, sample_id, target - tolerance, target + tolerance, target)
    ).fetchone()
    return row["sludge_value"] if row else None


def _classify_settling(sv30, sv90, floating):
    """Classify the test outcome from its shape."""
    if floating:
        return "rising"                       # floating sludge = rising
    if sv30 is not None and sv30 > 90:
        return "bulking"                       # barely settled
    if (sv30 is not None and sv90 is not None
            and (sv30 - sv90) < 2 and sv30 > 50):
        return "slow"                          # settled little after min 30
    return "healthy"


def _reached_30(conn, device_id, sample_id):
    """Did this test run long enough to be meaningful (≥30 min)?"""
    row = conn.execute(
        "SELECT MAX(minute) AS m FROM readings WHERE device_id=? AND sample_id=?",
        (device_id, sample_id)).fetchone()
    return row and row["m"] is not None and row["m"] >= MIN_VALID_MIN


def compute_sample_metrics(conn, device_id, sample_id):
    """Compute SV5/30/60/90 + class and upsert the samples row."""
    sv5  = _value_at_minute(conn, device_id, sample_id, 5)
    sv30 = _value_at_minute(conn, device_id, sample_id, 30)
    sv60 = _value_at_minute(conn, device_id, sample_id, 60)
    sv90 = _value_at_minute(conn, device_id, sample_id, 90)

    fl = conn.execute(
        "SELECT MIN(minute) AS m FROM readings "
        "WHERE device_id=? AND sample_id=? AND floating=1",
        (device_id, sample_id)).fetchone()
    floating_min = fl["m"] if fl else None
    floating = 1 if floating_min is not None else 0

    settling_class = _classify_settling(sv30, sv90, floating)

    times = conn.execute(
        "SELECT MIN(ts) AS s, MAX(ts) AS e FROM readings "
        "WHERE device_id=? AND sample_id=?",
        (device_id, sample_id)).fetchone()

    # UPSERT — idempotent: computing twice just overwrites with the same result.
    conn.execute(
        "INSERT INTO samples "
        "(device_id, sample_id, started_at, ended_at, status, "
        " sv5, sv30, sv60, sv90, settling_class, "
        " floating_detected, floating_first_minute) "
        "VALUES (?, ?, ?, ?, 'complete', ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(device_id, sample_id) DO UPDATE SET "
        " ended_at=excluded.ended_at, status='complete', "
        " sv5=excluded.sv5, sv30=excluded.sv30, sv60=excluded.sv60, "
        " sv90=excluded.sv90, settling_class=excluded.settling_class, "
        " floating_detected=excluded.floating_detected, "
        " floating_first_minute=excluded.floating_first_minute",
        (device_id, sample_id, times["s"], times["e"],
         sv5, sv30, sv60, sv90, settling_class, floating, floating_min))
    conn.commit()


def close_sample(conn, device_id, sample_id):
    """Close a test — but only keep it if it ran long enough to matter."""
    if not _reached_30(conn, device_id, sample_id):
        # Under 30 min: discard (keep raw readings, but no samples row).
        print(f"Sample {sample_id} under 30 min — discarded (not shown).")
        current_sample.pop(device_id, None)
        return
    compute_sample_metrics(conn, device_id, sample_id)
    current_sample.pop(device_id, None)
```

**The validity gate (`_reached_30`)** is important: a settling test that stopped
before minute 30 has no usable SV30, so it's noise. We keep the raw readings
(for debugging) but don't create a `samples` row, so it never clutters the
dashboard's history.

**`ON CONFLICT ... DO UPDATE`** makes `compute_sample_metrics` idempotent — if
it runs twice (e.g. closed by the new-sample trigger *and* the sweep), the second
run just overwrites with identical values instead of erroring.

### The sweep — a safety net

What if a device stops sending mid-test (power loss, network drop)? Triggers 1–3
never fire, so the sample would sit "running" forever. The sweep handles this:

```python
def sweep_stuck_samples(conn):
    """
    Periodically close tests that stopped sending. A test 'would' end at
    minute 100; if enough wall-clock time has passed since its last reading
    that it should have finished, close it now.
    """
    rows = conn.execute(
        "SELECT device_id, sample_id, MAX(minute) AS last_min, MAX(ts) AS last_ts "
        "FROM readings WHERE sample_id IS NOT NULL "
        "GROUP BY device_id, sample_id").fetchall()
    now = datetime.utcnow()
    for r in rows:
        # Skip samples already closed (have a complete row).
        done = conn.execute(
            "SELECT 1 FROM samples WHERE device_id=? AND sample_id=? AND status='complete'",
            (r["device_id"], r["sample_id"])).fetchone()
        if done:
            continue
        last_min = r["last_min"] or 0
        remaining = TEST_DURATION_MIN - last_min       # minutes left to "natural" end
        try:
            last_ts = datetime.fromisoformat(r["last_ts"])
        except (ValueError, TypeError):
            continue
        # If the test would have ended by now, close it.
        if now >= last_ts + timedelta(minutes=remaining):
            print(f"Sweep closing stuck sample {r['sample_id']} (last min {last_min}).")
            close_sample(conn, r["device_id"], r["sample_id"])
```

**Why this timing logic (not a fixed timeout):** a naïve "close after 10 min of
silence" would wrongly close a test that reconnects mid-run. Instead we wait
until the test *would have naturally finished* (minute 100). A test that dropped
at minute 20 waits ~80 minutes before being swept; one that dropped at minute 95
is swept in ~5. This lets brief reconnects resume rather than getting cut off.


---

## Part B — The Ingestion Service

`ingest.py` connects to AWS IoT Core over MQTT, subscribes to the telemetry
topic, and hands each message to `db.store_payload()`. It also runs the sweep
periodically on a background thread.

```python
import json
import ssl
import threading
import time
import paho.mqtt.client as mqtt
import db

# --- AWS IoT Core connection details ---
ENDPOINT   = "your-endpoint-ats.iot.ap-south-1.amazonaws.com"
PORT       = 8883
TOPIC      = "DL/63461537"                 # your per-unit topic
CLIENT_ID  = "flocdash-ingest-1"           # UNIQUE — avoids collision with production
CERT_DIR   = "/home/ubuntu/flocdashboard/certs"

def on_connect(client, userdata, flags, rc, properties=None):
    print("Connected to AWS IoT Core." if rc == 0 else f"Connect failed rc={rc}")
    client.subscribe(TOPIC, qos=1)
    print(f"Subscribing to '{TOPIC}'...")

def on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode("utf-8"))
        db.store_payload(payload)
        print(f"Stored reading: device payload state={payload.get('state')} "
              f"minute={payload.get('minute')}")
    except Exception as e:
        print(f"on_message error: {e}")

def sweep_loop():
    """Background thread: run the stuck-sample sweep every 2 minutes."""
    while True:
        time.sleep(120)
        try:
            conn = db.get_connection()
            db.sweep_stuck_samples(conn)
            conn.close()
        except Exception as e:
            print(f"sweep error: {e}")

def main():
    client = mqtt.Client(client_id=CLIENT_ID,
                         callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
    client.tls_set(
        ca_certs=f"{CERT_DIR}/AmazonRootCA1.pem",
        certfile=f"{CERT_DIR}/cert.pem.crt",
        keyfile=f"{CERT_DIR}/private.pem.key",
        tls_version=ssl.PROTOCOL_TLSv1_2)
    client.on_connect = on_connect
    client.on_message = on_message

    threading.Thread(target=sweep_loop, daemon=True).start()

    print(f"Connecting to {ENDPOINT} ...")
    client.connect(ENDPOINT, PORT, keepalive=60)
    client.loop_forever()        # blocks, auto-reconnects

if __name__ == "__main__":
    main()
```

**Key points:**

- **Unique `CLIENT_ID`** — MQTT brokers disconnect a client if another connects
  with the same id. Using `flocdash-ingest-1` (distinct from any production
  client) lets both subscribe simultaneously without kicking each other off.
- **`loop_forever()`** — handles reconnection automatically if the network drops.
- **The sweep on a daemon thread** — runs alongside message handling; `daemon=True`
  means it dies with the main process.
- **Certificates** — copy your AWS IoT device certs into `certs/`. The private
  key should be `chmod 600`.

Put your certs in place:
```bash
mkdir -p ~/flocdashboard/certs
# copy AmazonRootCA1.pem, cert.pem.crt, private.pem.key into it
chmod 600 ~/flocdashboard/certs/private.pem.key
```

Test it (with the venv active):
```bash
cd ~/flocdashboard && source venv/bin/activate
python ingest.py
```
You should see "Connected to AWS IoT Core" then "Stored reading:" lines as data
arrives.

---

## Part C — The Web Dashboard

`app.py` is the Flask application. It's the largest piece. Rather than reproduce
every line, this section explains its structure and the important patterns; the
full file is in your project.

### Structure

```
app.py
├── config loading (.env.s3) + app.secret_key
├── boto3 lazy client (get_s3)  — for presigned URLs
├── auth helpers (login_required, get_current_user, get_visible_devices)
├── routes:
│    ├── /login, /logout
│    ├── /                       fleet overview (device cards)
│    ├── /device/<id>            device detail page (HTML template)
│    ├── /api/device/<id>/latest       current/running sample + state
│    ├── /api/device/<id>/samples      list of completed samples
│    ├── /api/device/<id>/sample/<sid> one sample's curve + metrics
│    ├── /api/device/<id>/events/<sid> images + errors (with presigned URLs)
│    ├── /api/image/<id>/url            presigned URL for one image
│    └── /stream/device/<id>           SSE live updates
└── background _watch_db thread (feeds all SSE streams)
```

### Config and secret key — load order matters

```python
from flask import Flask
import os

app = Flask(__name__)

# Load secrets BEFORE using them.
_cfg = {}
try:
    for line in open("/home/ubuntu/flocdashboard/.env.s3"):
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.strip().split("=", 1)
            _cfg[k] = v
except FileNotFoundError:
    pass

# The session-signing key. From the env file, else a random per-process value.
app.secret_key = _cfg.get("FLASK_SECRET_KEY") or os.urandom(32).hex()
```

**Why load config before `app.secret_key`:** you can't set the key from config
you haven't read yet. Ordering bugs like this are easy to miss.

**Why the secret key matters:** Flask signs session cookies with it. If it's a
hardcoded public string, anyone can forge a logged-in session. A strong secret
from the env file prevents that. Downside: changing it logs everyone out once
(old cookies become invalid) — expected, one-time.


### Authentication and role-based access

```python
import functools
from flask import session, redirect, url_for
from werkzeug.security import check_password_hash

def login_required(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper

def get_current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    conn = db.get_connection()
    u = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    conn.close()
    return u

def get_visible_devices(conn, user):
    """Admins see all devices; regular users see only assigned ones."""
    if user["role"] == "admin":
        return conn.execute("SELECT * FROM devices ORDER BY id").fetchall()
    return conn.execute(
        "SELECT d.* FROM devices d "
        "JOIN user_devices ud ON ud.device_id = d.id "
        "WHERE ud.user_id = ? ORDER BY d.id", (user["id"],)).fetchall()

def device_allowed(conn, user, device_id):
    """Guard every device-specific endpoint against cross-user access."""
    if user["role"] == "admin":
        return True
    row = conn.execute(
        "SELECT 1 FROM user_devices WHERE user_id=? AND device_id=?",
        (user["id"], device_id)).fetchone()
    return row is not None
```

**Passwords are stored hashed** (via `werkzeug.security.generate_password_hash`),
never in plaintext. `check_password_hash` compares safely.

**`device_allowed` is called in every device endpoint** — never trust that the
UI hid a device; enforce access on the server for each request. A user could
otherwise just type another device's URL.

Create an admin user with a helper `create_user.py`:

```python
import sys, db
from werkzeug.security import generate_password_hash

username, password, role = sys.argv[1], sys.argv[2], sys.argv[3]
conn = db.get_connection()
conn.execute("INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
             (username, generate_password_hash(password), role))
conn.commit()
print(f"Created {role} user: {username}")
```
```bash
python3 create_user.py admin your_password admin
```

### The presigned-URL pattern for images

The S3 bucket is **private** — no public read access. To show an image in the
browser, generate a temporary signed URL that grants read access for ~15 minutes:

```python
def get_s3():
    """Lazy boto3 client — created on first use to keep startup light."""
    global _s3_client
    if _s3_client is None:
        import boto3
        _s3_client = boto3.client(
            "s3",
            aws_access_key_id=_cfg.get("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=_cfg.get("AWS_SECRET_ACCESS_KEY"),
            region_name=_cfg.get("AWS_REGION"))
    return _s3_client

# Inside an endpoint:
url = get_s3().generate_presigned_url(
    "get_object",
    Params={"Bucket": S3_BUCKET, "Key": image_row["s3_key"]},
    ExpiresIn=900)   # valid 15 minutes
```

**Why lazy-load boto3:** importing boto3 causes a one-time memory spike. On a
tight box, doing it at import time can push a process over the edge. Deferring
to first use spreads the cost. (In the *image service*, we do the opposite —
load at startup — because there the spike during a request was the problem.
Match the strategy to where the spike hurts least.)

### The events endpoint — placing icons on the curve

This endpoint returns everything to annotate on a sample's curve: image marks
and errors, each with a **minute** (where to draw it) and an **image_url**
(what to show on click).

```python
@app.route("/api/device/<int:device_id>/events/<sample_id>")
@login_required
def api_sample_events(device_id, sample_id):
    # ... access check ...
    events = []

    # Image marks — map the state to a minute.
    for im in images_for(conn, device_id, sample_id):
        state = im["state"] or ""
        minute = {"30Mark": 30, "60Mark": 60, "90Mark": 90}.get(state)
        if state == "floatingSludge":
            minute = first_floating_minute(conn, device_id, sample_id)
        url = presign(im["s3_key"])          # temporary S3 link
        events.append({"type": "floating" if state == "floatingSludge" else "image",
                       "minute": minute, "state": state,
                       "image_url": url, "detail": state})

    # Errors — derive a minute by matching the error's timestamp to the
    # nearest reading of this sample, and look up any error image.
    for er in errors_for(conn, device_id, sample_id):
        minute = nearest_reading_minute(conn, device_id, sample_id, er["ts"])
        err_img = image_for_state(conn, device_id, sample_id, "error")
        url = presign(err_img["s3_key"]) if err_img else None
        events.append({"type": "error", "minute": minute, "state": "error",
                       "image_url": url,
                       "detail": f'{er["error_code"]}: {er["message"]}'})
    return jsonify(events)
```

**The error-minute derivation is a nice trick:** error payloads don't include a
"minute" field, but they do have a timestamp. We find the reading whose timestamp
is closest to the error's, and use its minute. That places the error icon at the
right spot on the curve.

```sql
-- "nearest reading" query — order by absolute time difference
SELECT minute FROM readings
WHERE device_id=? AND sample_id=? AND minute IS NOT NULL
ORDER BY ABS(strftime('%s', ts) - strftime('%s', ?)) LIMIT 1
```


### Real-time updates with Server-Sent Events (SSE)

The dashboard shows a live-building curve. Rather than the browser polling every
second (wasteful), the server **pushes** updates over a long-lived connection
using SSE.

**The scaling problem:** if every open browser tab hit the database once per
second, N tabs = N queries/second. Instead, one **shared background thread**
checks the DB once per second and updates in-memory state; all SSE connections
read from that shared state.

```python
import threading, time, json
from flask import Response, stream_with_context

_latest_state = {}                # device_id -> latest reading info
_state_lock = threading.Lock()

def _watch_db():
    """One thread. Once/sec, refresh the latest reading per device."""
    conn = db.get_connection()
    while True:
        try:
            with _state_lock:
                for d in conn.execute("SELECT id FROM devices").fetchall():
                    row = conn.execute(
                        "SELECT id, sample_id, minute, state, sludge_value "
                        "FROM readings WHERE device_id=? ORDER BY id DESC LIMIT 1",
                        (d["id"],)).fetchone()
                    if row:
                        _latest_state[d["id"]] = {
                            "last_id": row["id"], "sample_id": row["sample_id"],
                            "minute": row["minute"], "state": row["state"],
                            "value": row["sludge_value"]}
        except Exception as e:
            print(f"watch_db error: {e}")
        time.sleep(1)

threading.Thread(target=_watch_db, daemon=True).start()

@app.route("/stream/device/<int:device_id>")
@login_required
def stream_device(device_id):
    # ... access check ...
    @stream_with_context
    def event_stream():
        last_sent = None
        while True:
            with _state_lock:
                state = _latest_state.get(device_id)
            if state and state["last_id"] != last_sent:
                last_sent = state["last_id"]
                yield f"data: {json.dumps(state)}\n\n"   # SSE frame format
            else:
                time.sleep(1)
                yield ": keepalive\n\n"                    # comment frame
    return Response(event_stream(), mimetype="text/event-stream")
```

**SSE frame format:** each message is `data: <payload>\n\n`. Lines starting with
`:` are comments (keepalives) that keep the connection open without triggering
the browser's `onmessage`.

**The browser side** (in the page's JavaScript):
```javascript
const evt = new EventSource(`/stream/device/${deviceId}`);
evt.onmessage = function(e){
  const d = JSON.parse(e.data);
  setPill(d.state);                     // update idle/running/error badge
  // append the new point to the live curve, etc.
};
```

### The idle-vs-running bug (an instructive one)

A subtle bug: `/latest` originally returned "the most recent sample that has
readings" — but didn't check whether the device was *still running a test*. When
a device went idle, the last test's readings were still "most recent," so the
dashboard thought a test was live and showed a phantom "running" curve.

**The fix:** `/latest` also reports the device's **current state** (from the
newest reading) and an `is_running` flag:

```python
latest_reading = conn.execute(
    "SELECT sample_id, state FROM readings WHERE device_id=? "
    "ORDER BY id DESC LIMIT 1", (device_id,)).fetchone()
current_state = latest_reading["state"] if latest_reading else "idle"
running = ("start","ongoing","30Mark","60Mark","90Mark","floatingSludge")
is_running = current_state in running
# ... return is_running in the JSON ...
```

The frontend enters "live view" only when `is_running` is true, and the SSE
handler drops out of live view when an idle/end state arrives.

**Lesson:** "latest data exists" is not the same as "a test is active." State
must be explicit, not inferred from the presence of data.


### Drawing labeled event tags on the curve

The curve is Chart.js. Event markers are labeled rounded-rectangle tags (`30`,
`60`, `90`, `F` for floating, `!` for error) drawn by a **custom Chart.js
plugin** that hooks the `afterDatasetsDraw` phase:

```javascript
const eventTagPlugin = {
  id: 'eventTags',
  afterDatasetsDraw(chart){
    const ev = chart.$events || [];
    const ctx = chart.ctx;
    chart.$tagBoxes = [];                      // store clickable regions
    ev.forEach(e => {
      if (e.minute == null) return;
      const x = chart.scales.x.getPixelForValue(e.minute);
      const y = chart.scales.y.getPixelForValue(e._y ?? 50);
      const text = eventLabel(e);              // '30' | 'F' | '!' ...
      // draw a rounded rect + pointer line + centered text (canvas API)
      // ...
      chart.$tagBoxes.push({x1, y1, x2, y2, _event: e});  // for hit-testing
    });
  }
};
```

Clicks are hit-tested against the stored boxes:
```javascript
options: {
  onClick: (evt) => {
    const rect = chart.canvas.getBoundingClientRect();
    const mx = evt.native.clientX - rect.left;
    const my = evt.native.clientY - rect.top;
    for (const b of (chart.$tagBoxes || []))
      if (mx>=b.x1 && mx<=b.x2 && my>=b.y1 && my<=b.y2){ openModal(b._event); return; }
  }
}
```

Clicking opens a modal showing the image (via the presigned `image_url`) or
"No image available", plus the event's details.

**Why a custom plugin instead of Chart.js point styles:** Chart.js can't draw
labeled boxes natively. The plugin gives full control over appearance and
positioning, and lets tags sit precisely at `(minute, curve-value)`.

### Sample navigation

The detail page lets you arrow between a device's completed tests:
- Fetch `/api/device/<id>/samples` (list, newest first)
- Left arrow = older, right = newer, showing "Sample X of Y"
- Completed samples are **immutable**, so once viewed they're cached in the
  browser — navigating back is instant, no re-fetch
- The X-axis is fixed 0–100 so every curve is on the same scale and comparable

---

## Part D — The Image Service + S3

`image_service.py` is a small standalone Flask app on port 5001. It receives an
image upload, stores it in S3, and records a row in `images`.

```python
import os, sqlite3
from datetime import datetime
from flask import Flask, request, jsonify

DB_FILE  = "/home/ubuntu/flocdashboard/flocdash.db"
ENV_FILE = "/home/ubuntu/flocdashboard/.env.s3"

_cfg = {}
for line in open(ENV_FILE):
    if "=" in line and not line.strip().startswith("#"):
        k, v = line.strip().split("=", 1); _cfg[k] = v

S3_BUCKET    = _cfg["S3_BUCKET"]
UPLOAD_TOKEN = _cfg.get("UPLOAD_TOKEN")     # shared secret

# Load boto3 at STARTUP here (not lazily) — see note below.
import boto3
_s3 = boto3.client("s3",
    aws_access_key_id=_cfg["AWS_ACCESS_KEY_ID"],
    aws_secret_access_key=_cfg["AWS_SECRET_ACCESS_KEY"],
    region_name=_cfg["AWS_REGION"])

app = Flask(__name__)

@app.route("/health")
def health():
    return jsonify({"status": "ok"})

@app.route("/upload", methods=["POST"])
def upload():
    # --- Auth: require the shared token ---
    if UPLOAD_TOKEN:
        token = request.headers.get("X-Upload-Token") or request.form.get("token")
        if token != UPLOAD_TOKEN:
            return jsonify({"ok": False, "error": "unauthorized"}), 401

    f = request.files.get("file")
    if not f:
        return jsonify({"ok": False, "error": "no file"}), 400

    plant_id  = request.form.get("plantId")
    plc_id    = request.form.get("plcId")
    sample_id = request.form.get("sampleId")
    state     = request.form.get("state")
    timestamp = request.form.get("timestamp")
    if not plant_id or not plc_id:
        return jsonify({"ok": False, "error": "missing plantId/plcId"}), 400

    conn = sqlite3.connect(DB_FILE)
    row = conn.execute("SELECT id FROM devices WHERE plant_id=? AND plc_id=?",
                       (plant_id, plc_id)).fetchone()
    if not row:
        conn.close()
        return jsonify({"ok": False, "error": "device not found"}), 404
    device_id = row[0]

    # Structured, debuggable S3 key.
    safe_state  = (state or "image").replace("/", "_")
    safe_sample = sample_id or "no_sample"
    s3_key = f"flocdetector/{plant_id}/{safe_sample}/{safe_state}.jpg"

    try:
        _s3.put_object(Bucket=S3_BUCKET, Key=s3_key,
                       Body=f.read(), ContentType="image/jpeg")
    except Exception as e:
        conn.close()
        return jsonify({"ok": False, "error": f"s3 upload failed: {e}"}), 500

    conn.execute(
        "INSERT INTO images (device_id, sample_id, state, s3_key, captured_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (device_id, sample_id, state, s3_key,
         timestamp or datetime.utcnow().isoformat()))
    conn.commit(); conn.close()
    return jsonify({"ok": True, "s3_key": s3_key, "device_id": device_id})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001)
```

**The S3 key structure** `flocdetector/<plant>/<sample>/<state>.jpg` makes the
bucket self-documenting — you can browse by plant, then by test, and see each
state's snapshot.

**Load boto3 at startup here** (unlike the dashboard's lazy load): the first
upload request was getting killed by the boto3 memory spike happening *during*
the request. Loading at boot moves that spike to a calm moment. This is the
opposite choice from the dashboard — because the spike hurts in a different place.

**Device must exist first:** the image service looks up the device by
plant+plc. That device row is created by the *ingestion* service when telemetry
first arrives. So telemetry registers the device; images attach to it.

### Testing the upload

`curl` on a tight box sometimes gets OOM-killed mid-request; the Python
`requests` library is more reliable for testing:

```python
import requests
r = requests.post('http://localhost:5001/upload',
  headers={'X-Upload-Token': 'YOUR_TOKEN'},
  files={'file': open('/tmp/test.jpg', 'rb')},
  data={'plantId':'...', 'plcId':'...', 'sampleId':'...', 'state':'30Mark'})
print(r.status_code, r.json())
```

Verify it landed in both places:
```bash
sqlite3 flocdash.db "SELECT * FROM images ORDER BY id DESC LIMIT 5;" -header -column
# and list the S3 bucket contents with a small boto3 script
```


---

## Part E — Node-RED Integration

The field unit's Node-RED already sends images to a production endpoint. We add
a **parallel branch** that sends a *copy* to our image service — without touching
the production flow.

### The builder function node

Add a Function node ("Build Image for My Service") that transforms the payload
into our service's expected form and attaches the auth token:

```javascript
// Only states that carry an image
const validStates = ["30Mark","60Mark","90Mark","floatingSludge","error"];
if (!validStates.includes(msg.payload.state) || !msg.payload.sampleId) return null;

// The image arrives as base64; convert to binary
let base64Image = msg.payload.base64_encoded;
const base64Data = base64Image?.split(",")?.[1] || base64Image || null;
let binaryData = null;
if (base64Data) {
    try { binaryData = Buffer.from(base64Data, "base64"); } catch (e) { binaryData = null; }
}
if (!binaryData) return null;      // our service requires a file

msg.payload = {
    file: { value: binaryData,
            options: { filename: msg.payload.image_file || "image.jpg",
                       contentType: "image/jpeg" } },
    plantId:  env.get('plantId'),   // from the flow's env vars
    plcId:    env.get('plcId'),
    sampleId: msg.payload.sampleId,
    state:    msg.payload.state,
    timestamp: msg.time
};
msg.headers = { "X-Upload-Token": "YOUR_TOKEN" };   // auth
return msg;
```

### The HTTP request node

Add an "http request" node:
- Method: **POST**
- URL: `http://<your-ec2-ip>:5001/upload`
- Body: `multipart/form-data`

The `file: { value, options }` structure tells Node-RED's HTTP node to send that
field as a file part.

### Wiring

```
[Manage State] ─┬─► [production image builder] ─► [production HTTP POST]   (existing)
                └─► [Build Image for My Service] ─► [POST to My Service]    (new)
```

Wire the new branch from the **same source** as the existing image branch, so it
runs in parallel. Production is untouched.

**Why plantId/plcId from env:** production's image payload identifies the device
by an `assetId`, but *our* service demuxes by (plantId, plcId). Those are
available as Node-RED flow environment variables (set during provisioning), so
we read them with `env.get()`.

---

## Part F — Deployment with PM2

PM2 keeps the three processes running permanently and restarts them on crash or
reboot.

### The gotcha — PM2 and Python

PM2 auto-detects how to run a file, and on some installs it mis-handles `.py` and
`.sh` files (trying to run them through a JavaScript engine, producing bizarre
"unterminated string literal" errors). The reliable fix: **bash wrapper scripts**
run explicitly with `--interpreter bash`.

Create a wrapper per service:

```bash
cat > ~/flocdashboard/start_ingest.sh << 'EOF'
#!/bin/bash
cd /home/ubuntu/flocdashboard
source venv/bin/activate
exec python ingest.py
EOF
chmod +x ~/flocdashboard/start_ingest.sh
```

Repeat for `start_web.sh` (runs `app.py`) and `start_image.sh` (runs
`image_service.py`).

**Why the wrapper:** it activates the venv (so the right packages are found) then
`exec`s python (so PM2 tracks the python process directly, not a wrapping shell).

### Start the services

```bash
pm2 start ~/flocdashboard/start_ingest.sh --name flocdash-ingest --interpreter bash
pm2 start ~/flocdashboard/start_web.sh    --name flocdash-web    --interpreter bash
pm2 start ~/flocdashboard/start_image.sh  --name flocdash-image  --interpreter bash

pm2 list          # all three should be 'online'
pm2 save          # remember this process list
pm2 startup       # print a command to enable start-on-boot — run that command
```

### Freeing RAM on a constrained box

Disable services you don't need to make headroom:
```bash
sudo systemctl disable --now multipathd ModemManager unattended-upgrades
free -h            # confirm available RAM
```

### Security groups (firewall)

If Node-RED runs off-box (e.g. on the Pi), open the image service port in the
EC2 security group:
- Inbound rule: Custom TCP, port **5001**, source `0.0.0.0/0`
  (the Pi's IP may change, so you can't easily restrict it)
- Port **5000** for the dashboard

If Node-RED runs *on* the same box, it can POST to `localhost:5001` and no
inbound rule is needed for 5001.

### Deploying updates

Edit locally, upload with `scp`, restart the affected service:
```bash
scp -i "your-key.pem" app.py ubuntu@<ec2-dns>:~/flocdashboard/app.py
# on the box:
python3 -c "import ast; ast.parse(open('app.py').read()); print('OK')"   # syntax check
pm2 restart flocdash-web
```

Always syntax-check before restarting so you don't take a service down with a
typo.

---

## Part G — Security

The system is exposed on public ports, so it needs baseline protections.

### 1. Upload token (protects the image endpoint)

The image service rejects any upload without the correct token:
```python
if UPLOAD_TOKEN:
    token = request.headers.get("X-Upload-Token") or request.form.get("token")
    if token != UPLOAD_TOKEN:
        return jsonify({"ok": False, "error": "unauthorized"}), 401
```
Node-RED includes the token as `X-Upload-Token`. Without it, uploads get 401.
Generate the token with `secrets.token_urlsafe(32)` and store it in `.env.s3`.

### 2. Flask secret key (protects login sessions)

Signs session cookies so they can't be forged. Loaded from `.env.s3`, never
hardcoded. See Part C.

### 3. Hashed passwords

User passwords are stored as hashes (`werkzeug.security`), never plaintext.

### What's still open (documented honestly)

- **HTTPS** — the dashboard (including the login form) runs over plain HTTP, so
  passwords travel unencrypted. For production, put it behind a reverse proxy
  (Caddy/nginx) with a TLS certificate, or an AWS load balancer. This is the
  most important remaining hardening.
- **Production WSGI server** — Flask's dev server isn't built for production
  load. Swap in `gunicorn` when you outgrow the prototype.

---

## Troubleshooting & Lessons Learned

**"Killed" during an operation** — usually the kernel's out-of-memory killer, but
if `dmesg | grep -i "killed process"` is empty, it may be a transient spike that
killed an expendable process (like `curl`) rather than a hard OOM. Retry; if it
succeeds, it was a one-time spike (often boto3 loading). Fix by loading heavy
libs at a calm moment (startup) rather than during a request.

**PM2 "unterminated string literal"** — PM2 tried to run your Python/shell file
through its JS engine. Use a bash wrapper + `--interpreter bash`.

**venv python symlinks to system python** — confusing, but harmless: activation
sets the path so the venv's `site-packages` are found. For PM2, activate the
venv inside the wrapper script rather than pointing at a python binary directly.

**Phantom "running" state** — don't infer "a test is active" from "recent data
exists." Track and report the device's explicit current state.

**"minute undefined" in the UI** — data-shape mismatch: one endpoint returned
`{minute, value}` while the code read `.x`/`.y`. Keep your data shapes consistent
across endpoints, or convert explicitly.

**Duplicate readings on reconnect** — the `UNIQUE(device_id, sample_id, minute,
state)` constraint plus catching `IntegrityError` makes inserts idempotent.

**Sample never closes** — the sweep is the safety net; it closes tests that
stopped sending, timed to when they *would* have naturally ended.

**Matching the datastore/framework to the hardware** — the biggest recurring
theme. SQLite over Postgres, three light Flask processes over one heavy FastAPI,
lazy-vs-eager library loading. On constrained hardware, the pragmatic choice
that runs reliably beats the "better" choice that OOMs.

---

## Future Hardening

Roughly in priority order:

1. **HTTPS / reverse proxy** — encrypt the login and all traffic
2. **gunicorn** — production WSGI server instead of the Flask dev server
3. **Trends view** — SV30 across days/weeks per unit, for early bulking warnings
4. **Alerts** — notify on rising SV30, bulking, unit offline, floating detected
5. **DB backups** — periodic copy of `flocdash.db` (e.g. to S3)
6. **Rate limiting** — on the upload and login endpoints
7. **Log rotation** — PM2 logs grow; configure `pm2-logrotate`
8. **Cleanup** — replace deprecated `datetime.utcnow()` with
   `datetime.now(datetime.UTC)`

---

## Appendix — File Manifest

```
~/flocdashboard/
├── venv/                    # Python virtual environment
├── .env.s3                  # secrets (chmod 600) — AWS keys, tokens
├── certs/                   # AWS IoT device certificates (key chmod 600)
│   ├── AmazonRootCA1.pem
│   ├── cert.pem.crt
│   └── private.pem.key
├── schema.sql               # database schema
├── flocdash.db              # the SQLite database
├── db.py                    # data-access layer (shared)
├── ingest.py                # MQTT ingestion service        (PM2: flocdash-ingest)
├── app.py                   # Flask dashboard               (PM2: flocdash-web)
├── image_service.py         # image upload service          (PM2: flocdash-image)
├── create_user.py           # helper to add users
├── migrate_errors.py        # one-off migration (adds sample_id to device_errors)
├── start_ingest.sh          # PM2 wrapper scripts
├── start_web.sh
└── start_image.sh
```

### Data flow, end to end

1. Field unit measures sludge each minute → publishes MQTT to AWS IoT Core
2. `ingest.py` receives it → `db.store_payload()` → row in `readings`
3. At key minutes / test end → metrics computed → row in `samples`
4. Field unit captures a snapshot → Node-RED POSTs it → `image_service.py`
   → stored in S3, row in `images`
5. User opens the dashboard → `app.py` reads the DB → renders the curve
6. SSE pushes new readings live → the curve grows in real time
7. User clicks an event tag → `app.py` generates a presigned S3 URL → the
   image shows in a modal

---

*This system was built incrementally, testing each piece before moving on, and
adapting decisions to real hardware constraints. That approach — build a little,
verify, learn, adjust — is the real lesson worth carrying to the next project.*
