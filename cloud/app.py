"""
app.py — Flask web app for the FlocDetector dashboard.
Stage B part 2: fleet overview page with live device status.
"""

import functools
import json
import time
import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from flask import (Flask, request, session, redirect, url_for,
                   render_template_string, flash, Response, stream_with_context)
from werkzeug.security import check_password_hash


def to_local(utc_str, tz_name):
    """
    Convert a stored UTC timestamp string to a device's local timezone.
    Returns 'YYYY-MM-DD HH:MM:SS' in the device's zone, or the original
    string if parsing fails. tz_name is an IANA name like 'Asia/Kolkata'.
    """
    if not utc_str:
        return utc_str
    try:
        # Parse the stored ISO string (naive = UTC by our convention).
        dt = datetime.fromisoformat(utc_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone(ZoneInfo(tz_name or "UTC"))
        return local.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return utc_str


def device_timezone(conn, device_id):
    """Fetch a device's IANA timezone name (defaults to 'UTC')."""
    row = conn.execute("SELECT timezone FROM devices WHERE id=?",
                       (device_id,)).fetchone()
    return (row["timezone"] if row and row["timezone"] else "UTC")

import db

app = Flask(__name__)

OFFLINE_THRESHOLD_MIN = 5   # no data for 5+ min = offline

# ---------------- config / secrets (loaded from .env.s3) ----------------
_s3_cfg = {}
try:
    for _line in open("/home/ubuntu/flocdashboard/.env.s3"):
        if "=" in _line and not _line.strip().startswith("#"):
            _k, _v = _line.strip().split("=", 1)
            _s3_cfg[_k] = _v
except FileNotFoundError:
    pass

# Session-signing secret. Loaded from the env file; falls back to a random
# per-process value if missing (which would log everyone out on restart —
# so set FLASK_SECRET_KEY in .env.s3 for stable sessions).
import os as _os
app.secret_key = _s3_cfg.get("FLASK_SECRET_KEY") or _os.urandom(32).hex()

_s3_client = None
def get_s3():
    """Create the boto3 S3 client on first use (lazy = lighter startup)."""
    global _s3_client
    if _s3_client is None:
        import boto3
        _s3_client = boto3.client(
            "s3",
            aws_access_key_id=_s3_cfg.get("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=_s3_cfg.get("AWS_SECRET_ACCESS_KEY"),
            region_name=_s3_cfg.get("AWS_REGION"),
        )
    return _s3_client

S3_BUCKET = _s3_cfg.get("S3_BUCKET")


# --------------------------- auth helpers ---------------------------

def login_required(f):
    @functools.wraps(f)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapped


def get_current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    conn = db.get_connection()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    conn.close()
    return row


def get_visible_devices(conn, user):
    """
    Admins see every device. Regular users see only devices listed for
    them in user_devices. This is the permission rule in action.
    """
    if user["role"] == "admin":
        return conn.execute("SELECT * FROM devices ORDER BY id").fetchall()
    return conn.execute(
        """SELECT d.* FROM devices d
           JOIN user_devices ud ON ud.device_id = d.id
           WHERE ud.user_id = ?
           ORDER BY d.id""",
        (user["id"],)).fetchall()


def minutes_since(ts_str):
    """How many minutes since this ISO timestamp string. None if no timestamp."""
    if not ts_str:
        return None
    try:
        ts = datetime.fromisoformat(ts_str)
    except Exception:
        return None
    return (datetime.utcnow() - ts).total_seconds() / 60.0


# --------------------------- login / logout ---------------------------

LOGIN_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Login — FlocDetector</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body{margin:0;background:#0d1b2a;color:#e8eef4;
       font-family:system-ui,sans-serif;display:flex;height:100vh;
       align-items:center;justify-content:center}
  .box{background:#11263b;border:1px solid #27425c;border-radius:12px;
       padding:36px 32px;width:320px}
  h1{margin:0 0 6px;font-size:20px}
  p.sub{margin:0 0 22px;color:#8aa0b4;font-size:13px}
  label{display:block;font-size:12px;color:#8aa0b4;margin:14px 0 4px}
  input{width:100%;box-sizing:border-box;background:#0a1722;
        border:1px solid #27425c;border-radius:6px;color:#e8eef4;
        padding:10px 12px;font-size:14px}
  input:focus{outline:none;border-color:#36c2a8}
  button{width:100%;margin-top:22px;background:#36c2a8;color:#04231c;
         border:none;border-radius:7px;padding:12px;font-size:15px;
         font-weight:600;cursor:pointer}
  .flash{background:#3b1f12;border:1px solid #e0a23a;color:#f3d9b0;
         border-radius:6px;padding:10px 12px;font-size:13px;margin-top:16px}
</style></head>
<body>
  <div class="box">
    <h1>FlocDetector</h1>
    <p class="sub">Sign in to the monitoring dashboard</p>
    <form method="post">
      <label>Username</label>
      <input name="username" autofocus>
      <label>Password</label>
      <input name="password" type="password">
      <button type="submit">Sign in</button>
    </form>
    {% with msgs = get_flashed_messages() %}
      {% for m in msgs %}<div class="flash">{{ m }}</div>{% endfor %}
    {% endwith %}
  </div>
</body></html>"""


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        conn = db.get_connection()
        user = conn.execute("SELECT * FROM users WHERE username = ?",
                            (username,)).fetchone()
        conn.close()
        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            session["role"] = user["role"]
            return redirect(url_for("home"))
        flash("Invalid username or password")
        return redirect(url_for("login"))
    return render_template_string(LOGIN_PAGE)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# --------------------------- fleet overview ---------------------------

FLEET_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>FlocDetector Dashboard</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root{--ink:#0d1b2a;--panel:#11263b;--line:#27425c;--text:#e8eef4;
        --muted:#8aa0b4;--good:#36c2a8;--bad:#e0555a;--warn:#e0a23a}
  *{box-sizing:border-box}
  body{margin:0;background:var(--ink);color:var(--text);
       font-family:system-ui,sans-serif}
  header{background:var(--panel);border-bottom:1px solid var(--line);
         padding:16px 24px;display:flex;align-items:center;gap:14px}
  header h1{margin:0;font-size:18px}
  header a{margin-left:auto;color:var(--muted);text-decoration:none;font-size:14px}
  main{padding:28px 24px;max-width:1100px;margin:0 auto}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));
        gap:16px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:10px;
        padding:18px 20px;text-decoration:none;color:var(--text);display:block}
  .card:hover{border-color:var(--good)}
  .card-top{display:flex;justify-content:space-between;align-items:center;
            margin-bottom:10px}
  .dot{width:10px;height:10px;border-radius:50%;display:inline-block;
       margin-right:6px}
  .dot.online{background:var(--good)}
  .dot.offline{background:var(--bad)}
  .name{font-weight:600;font-size:15px}
  .row{display:flex;justify-content:space-between;font-size:13px;
       color:var(--muted);margin-top:6px}
  .row b{color:var(--text)}
  .badge{display:inline-block;padding:2px 8px;border-radius:5px;font-size:11px;
         font-weight:600}
  .badge.healthy{background:#0f3b30;color:#bff3e6}
  .badge.slow{background:#3b3312;color:#f3e6b0}
  .badge.bulking{background:#3b1f12;color:#f3d9b0}
  .badge.rising{background:#3b1212;color:#f3b0b0}
  .empty{color:var(--muted);text-align:center;padding:60px 0}
</style></head>
<body>
  <header>
    <h1>FlocDetector Dashboard</h1>
    <a href="{{ url_for('logout') }}">Logout ({{ user['username'] }})</a>
  </header>
  <main>
    {% if devices %}
    <div class="grid">
      {% for d in devices %}
      <a class="card" href="{{ url_for('device_page', device_id=d.device['id']) }}">
        <div class="card-top">
          <span class="name">{{ d.device['name'] or ('Device ' ~ d.device['id']) }}</span>
          <span><span class="dot {{ 'online' if d.online else 'offline' }}"></span>
                {{ 'Online' if d.online else 'Offline' }}</span>
        </div>
        <div class="row"><span>{{ 'Current state' if d.online else 'State' }}</span><b>{{ d.latest_state or '—' }}</b></div>
        <div class="row"><span>Latest reading</span><b>{{ d.latest_sludge if d.latest_sludge is not none else '—' }}%</b></div>
        <div class="row"><span>Last sample SV30</span>
          <b>{{ d.sv30 if d.sv30 is not none else '—' }}
             {% if d.settling_class %}<span class="badge {{ d.settling_class }}">{{ d.settling_class }}</span>{% endif %}
          </b>
        </div>
        <div class="row"><span>Last test</span><b>{{ d.last_test or '—' }}</b></div>
      </a>
      {% endfor %}
    </div>
    {% else %}
      <div class="empty">No devices assigned to your account yet.</div>
    {% endif %}
  </main>
</body></html>"""


@app.route("/")
@login_required
def home():
    user = get_current_user()
    conn = db.get_connection()
    devices = get_visible_devices(conn, user)

    device_cards = []
    for dev in devices:
        # Latest reading (any state) for this device.
        latest = conn.execute(
            """SELECT state, sludge_value FROM readings
               WHERE device_id = ? ORDER BY id DESC LIMIT 1""",
            (dev["id"],)).fetchone()

        # Latest completed sample's headline numbers + when it finished.
        sample = conn.execute(
            """SELECT sv30, settling_class, started_at, ended_at FROM samples
               WHERE device_id = ? AND status = 'complete'
               ORDER BY id DESC LIMIT 1""",
            (dev["id"],)).fetchone()

        mins = minutes_since(dev["last_seen"])
        online = mins is not None and mins <= OFFLINE_THRESHOLD_MIN

        # When a device is offline, its last reading's state (e.g. "ongoing")
        # is stale — it's not actually running now. Present it as the LAST
        # known state rather than the current one, to avoid the contradictory
        # "Offline / ongoing" display.
        raw_state = latest["state"] if latest else None
        if online:
            display_state = raw_state or "—"
        else:
            display_state = f"Last: {raw_state}" if raw_state else "—"

        # When the last completed test finished, in the device's local time.
        tz = dev["timezone"] if dev["timezone"] else "UTC"
        last_test = None
        if sample and sample["ended_at"]:
            last_test = to_local(sample["ended_at"], tz)
        elif sample and sample["started_at"]:
            last_test = to_local(sample["started_at"], tz)

        device_cards.append({
            "device": dev,
            "online": online,
            "latest_state": display_state,
            "latest_sludge": latest["sludge_value"] if latest else None,
            "sv30": sample["sv30"] if sample else None,
            "settling_class": sample["settling_class"] if sample else None,
            "last_test": last_test,
        })
    conn.close()
    return render_template_string(FLEET_PAGE, user=user, devices=device_cards)


# ============================================================================
# These routes REPLACE the placeholder device_page() in app.py, and add a
# JSON API endpoint the chart calls. Add `import json` at the top of app.py.
# ============================================================================


def device_allowed(conn, user, device_id):
    """Check this user is allowed to view this device (admin = all)."""
    if user["role"] == "admin":
        return True
    row = conn.execute(
        "SELECT 1 FROM user_devices WHERE user_id = ? AND device_id = ?",
        (user["id"], device_id)).fetchone()
    return row is not None


@app.route("/device/<int:device_id>")
@login_required
def device_page(device_id):
    user = get_current_user()
    conn = db.get_connection()
    if not device_allowed(conn, user, device_id):
        conn.close()
        return "Not authorized to view this device.", 403
    dev = conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
    conn.close()
    if not dev:
        return "Device not found.", 404
    return render_template_string(DEVICE_PAGE, user=user, dev=dev)


@app.route("/api/device/<int:device_id>/latest")
@login_required
def api_latest_sample(device_id):
    """
    Returns JSON for the most recent sample of this device:
      - the curve points (minute, sludge_value)
      - the computed metrics
    The chart on the detail page fetches this.
    """
    from flask import jsonify
    user = get_current_user()
    conn = db.get_connection()
    if not device_allowed(conn, user, device_id):
        conn.close()
        return jsonify({"error": "forbidden"}), 403

    # Find the most recent reading overall — tells us the device's CURRENT state.
    latest_reading = conn.execute(
        """SELECT sample_id, state FROM readings
           WHERE device_id = ?
           ORDER BY id DESC LIMIT 1""",
        (device_id,)).fetchone()

    current_state = latest_reading["state"] if latest_reading else "idle"
    running_states = ("start", "ongoing", "30Mark", "60Mark", "90Mark", "floatingSludge")
    is_running = current_state in running_states

    # Find the most recent sample_id for this device that has readings.
    latest = conn.execute(
        """SELECT sample_id FROM readings
           WHERE device_id = ? AND sample_id IS NOT NULL
           ORDER BY id DESC LIMIT 1""",
        (device_id,)).fetchone()

    if not latest:
        conn.close()
        return jsonify({"sample_id": None, "points": [], "metrics": None,
                        "state": current_state, "is_running": False})

    sample_id = latest["sample_id"]

    # All readings for that sample, in minute order — the curve.
    rows = conn.execute(
        """SELECT minute, sludge_value FROM readings
           WHERE device_id = ? AND sample_id = ?
           AND minute IS NOT NULL AND sludge_value IS NOT NULL
           ORDER BY minute ASC""",
        (device_id, sample_id)).fetchall()
    points = [{"minute": r["minute"], "value": r["sludge_value"]} for r in rows]

    # The computed metrics row, if the sample has completed.
    s = conn.execute(
        """SELECT sv5, sv30, sv60, sv90, initial_velocity, compaction_ratio,
                  settling_class, floating_detected, floating_first_minute,
                  status FROM samples
           WHERE device_id = ? AND sample_id = ?""",
        (device_id, sample_id)).fetchone()
    metrics = dict(s) if s else None

    conn.close()
    return jsonify({"sample_id": sample_id, "points": points, "metrics": metrics,
                    "state": current_state, "is_running": is_running})


def _sample_payload(conn, device_id, sample_id):
    """Build the curve points + metrics for one sample (shared helper)."""
    rows = conn.execute(
        """SELECT minute, sludge_value FROM readings
           WHERE device_id = ? AND sample_id = ?
           AND minute IS NOT NULL AND sludge_value IS NOT NULL
           ORDER BY minute ASC""",
        (device_id, sample_id)).fetchall()
    points = [{"minute": r["minute"], "value": r["sludge_value"]} for r in rows]
    s = conn.execute(
        """SELECT sv5, sv30, sv60, sv90, initial_velocity, compaction_ratio,
                  settling_class, floating_detected, floating_first_minute,
                  status FROM samples
           WHERE device_id = ? AND sample_id = ?""",
        (device_id, sample_id)).fetchone()
    metrics = dict(s) if s else None
    return {"sample_id": sample_id, "points": points, "metrics": metrics}


@app.route("/api/device/<int:device_id>/samples")
@login_required
def api_sample_list(device_id):
    """List of this device's completed samples, newest first, for navigation."""
    from flask import jsonify
    user = get_current_user()
    conn = db.get_connection()
    if not device_allowed(conn, user, device_id):
        conn.close()
        return jsonify({"error": "forbidden"}), 403
    rows = conn.execute(
        """SELECT sample_id, started_at, ended_at, sv30, settling_class, status
           FROM samples WHERE device_id = ?
           ORDER BY started_at DESC""",
        (device_id,)).fetchall()
    tz = device_timezone(conn, device_id)
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["started_at"] = to_local(d["started_at"], tz)
        d["ended_at"] = to_local(d["ended_at"], tz)
        out.append(d)
    return jsonify(out)


@app.route("/api/device/<int:device_id>/sample/<sample_id>")
@login_required
def api_one_sample(device_id, sample_id):
    """One specific sample's curve + metrics (for arrow navigation)."""
    from flask import jsonify
    user = get_current_user()
    conn = db.get_connection()
    if not device_allowed(conn, user, device_id):
        conn.close()
        return jsonify({"error": "forbidden"}), 403
    payload = _sample_payload(conn, device_id, sample_id)
    conn.close()
    return jsonify(payload)


@app.route("/api/device/<int:device_id>/events/<sample_id>")
@login_required
def api_sample_events(device_id, sample_id):
    """
    Returns the events for a sample to place as icons on the curve:
      - images (SV30/60/90 marks, floating) with a presigned S3 URL
      - errors that occurred during this sample, with a derived minute
    Each event has: type, minute, state/code, image_url (or null), detail.
    """
    from flask import jsonify
    user = get_current_user()
    conn = db.get_connection()
    if not device_allowed(conn, user, device_id):
        conn.close()
        return jsonify({"error": "forbidden"}), 403

    events = []
    tz = device_timezone(conn, device_id)   # this device's local timezone

    # Helper: minute of a reading matching a state, for placing an icon.
    def minute_for_state(st):
        r = conn.execute(
            """SELECT minute FROM readings
               WHERE device_id = ? AND sample_id = ? AND state = ?
               AND minute IS NOT NULL ORDER BY minute LIMIT 1""",
            (device_id, sample_id, st)).fetchone()
        return r["minute"] if r else None

    # ---- Images (each has an S3 key; generate a presigned URL) ----
    imgs = conn.execute(
        """SELECT id, state, s3_key, captured_at FROM images
           WHERE device_id = ? AND sample_id = ?""",
        (device_id, sample_id)).fetchall()
    for im in imgs:
        # Map the image state to a minute on the curve.
        state = im["state"] or ""
        if state == "30Mark":   minute = 30
        elif state == "60Mark": minute = 60
        elif state == "90Mark": minute = 90
        elif state == "floatingSludge":
            fr = conn.execute(
                """SELECT minute FROM readings WHERE device_id=? AND sample_id=?
                   AND floating=1 ORDER BY minute LIMIT 1""",
                (device_id, sample_id)).fetchone()
            minute = fr["minute"] if fr else minute_for_state("floatingSludge")
        else:
            minute = minute_for_state(state)

        # Presigned URL (valid ~15 min) so the private image can be shown.
        url = None
        try:
            url = get_s3().generate_presigned_url(
                "get_object",
                Params={"Bucket": S3_BUCKET, "Key": im["s3_key"]},
                ExpiresIn=900)
        except Exception:
            url = None

        etype = "floating" if state == "floatingSludge" else "image"
        events.append({
            "type": etype,
            "minute": minute,
            "state": state,
            "image_url": url,
            "detail": state,
            "captured_at": to_local(im["captured_at"], tz),
        })

    # ---- Errors during this sample ----
    errs = conn.execute(
        """SELECT error_code, message, ts FROM device_errors
           WHERE device_id = ? AND sample_id = ?""",
        (device_id, sample_id)).fetchall()
    for er in errs:
        # Derive a minute: match error time to the nearest reading of this sample.
        minute = None
        r = conn.execute(
            """SELECT minute FROM readings
               WHERE device_id=? AND sample_id=? AND minute IS NOT NULL
               ORDER BY ABS(strftime('%s', ts) - strftime('%s', ?)) LIMIT 1""",
            (device_id, sample_id, er["ts"])).fetchone()
        if r:
            minute = r["minute"]

        # An error may have an image (state='error' in the images table).
        err_url = None
        img = conn.execute(
            """SELECT s3_key FROM images
               WHERE device_id=? AND sample_id=? AND state='error'
               ORDER BY id DESC LIMIT 1""",
            (device_id, sample_id)).fetchone()
        if img:
            try:
                err_url = get_s3().generate_presigned_url(
                    "get_object",
                    Params={"Bucket": S3_BUCKET, "Key": img["s3_key"]},
                    ExpiresIn=900)
            except Exception:
                err_url = None

        events.append({
            "type": "error",
            "minute": minute,
            "state": "error",
            "image_url": err_url,
            "detail": f'{er["error_code"]}: {er["message"]}',
            "captured_at": to_local(er["ts"], tz),
        })

    conn.close()
    return jsonify(events)


@app.route("/api/image/<int:image_id>/url")
@login_required
def api_image_url(image_id):
    """Generate a fresh presigned URL for a specific image id."""
    from flask import jsonify
    user = get_current_user()
    conn = db.get_connection()
    im = conn.execute("SELECT device_id, s3_key FROM images WHERE id = ?",
                      (image_id,)).fetchone()
    if not im or not device_allowed(conn, user, im["device_id"]):
        conn.close()
        return jsonify({"error": "forbidden"}), 403
    conn.close()
    try:
        url = get_s3().generate_presigned_url(
            "get_object",
            Params={"Bucket": S3_BUCKET, "Key": im["s3_key"]},
            ExpiresIn=900)
        return jsonify({"url": url})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/upload", methods=["POST"])
def upload_image():
    """
    Receives an image from the device/Node-RED (multipart form-data),
    uploads it to S3, and records a row in the images table.

    Form fields: file, plantId, plcId, sampleId, state, timestamp
    Note: no login_required — devices post here directly. Protected instead
    by being an internal endpoint; add a shared secret later if exposing it.
    """
    from flask import jsonify
    f = request.files.get("file")
    if not f:
        return jsonify({"ok": False, "error": "no file"}), 400

    plant_id = request.form.get("plantId")
    plc_id   = request.form.get("plcId")
    sample_id = request.form.get("sampleId")
    state    = request.form.get("state")
    timestamp = request.form.get("timestamp")

    if not plant_id or not plc_id:
        return jsonify({"ok": False, "error": "missing plantId/plcId"}), 400

    conn = db.get_connection()
    dev = conn.execute("SELECT id FROM devices WHERE plant_id = ? AND plc_id = ?",
                       (plant_id, plc_id)).fetchone()
    if not dev:
        conn.close()
        return jsonify({"ok": False, "error": "device not found"}), 404
    device_id = dev["id"]

    # Structured S3 key.
    safe_state = (state or "image").replace("/", "_")
    safe_sample = sample_id or "no_sample"
    s3_key = f"flocdetector/{plant_id}/{safe_sample}/{safe_state}.jpg"

    # Upload bytes to S3.
    try:
        get_s3().put_object(
            Bucket=S3_BUCKET, Key=s3_key,
            Body=f.read(), ContentType="image/jpeg")
    except Exception as e:
        conn.close()
        return jsonify({"ok": False, "error": f"s3 upload failed: {e}"}), 500

    now = datetime.utcnow().isoformat()
    conn.execute(
        """INSERT INTO images (device_id, sample_id, state, s3_key, captured_at)
           VALUES (?, ?, ?, ?, ?)""",
        (device_id, sample_id, state, s3_key, timestamp or now))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "s3_key": s3_key, "device_id": device_id})


DEVICE_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>{{ dev['name'] or 'Device' }} — FlocDetector</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  :root{--ink:#0d1b2a;--panel:#11263b;--line:#27425c;--text:#e8eef4;
        --muted:#8aa0b4;--good:#36c2a8;--bad:#e0555a;--warn:#e0a23a}
  *{box-sizing:border-box}
  body{margin:0;background:var(--ink);color:var(--text);font-family:system-ui,sans-serif}
  header{background:var(--panel);border-bottom:1px solid var(--line);
         padding:16px 24px;display:flex;align-items:center;gap:14px}
  header h1{margin:0;font-size:18px;display:flex;align-items:center}
  header a{color:var(--muted);text-decoration:none;font-size:14px}
  header a.back{margin-right:auto}
  header a.logout{margin-left:auto}
  main{padding:24px;max-width:1000px;margin:0 auto}
  .chart-card{background:var(--panel);border:1px solid var(--line);
              border-radius:10px;padding:20px;margin-bottom:20px}
  .nav{display:flex;align-items:center;justify-content:center;gap:16px;margin-bottom:12px}
  .nav button{background:#1c3247;color:var(--text);border:1px solid var(--line);
              border-radius:6px;width:34px;height:34px;font-size:16px;cursor:pointer}
  .nav button:hover:not(:disabled){border-color:var(--good)}
  .nav button:disabled{opacity:0.35;cursor:default}
  .nav .label{font-size:13px;color:var(--muted);text-align:center;min-width:260px}
  .newdata{display:none;background:#0f3b30;color:#bff3e6;border:1px solid var(--good);
           border-radius:6px;padding:4px 10px;font-size:12px;cursor:pointer;margin-left:10px}
  .metrics{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px}
  .metric{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:14px 16px}
  .metric .label{font-size:12px;color:var(--muted)}
  .metric .value{font-size:22px;font-weight:600;margin-top:4px}
  .badge{display:inline-block;padding:3px 10px;border-radius:6px;font-size:13px;font-weight:600;margin-top:4px}
  .badge.healthy{background:#0f3b30;color:#bff3e6}
  .badge.slow{background:#3b3312;color:#f3e6b0}
  .badge.bulking{background:#3b1f12;color:#f3d9b0}
  .badge.rising{background:#3b1212;color:#f3b0b0}
  .badge.unknown{background:#2a2a2a;color:#aaa}
  .sub{color:var(--muted);font-size:13px;margin:0 0 14px;text-align:center}
  .pill{display:inline-flex;align-items:center;gap:7px;padding:5px 12px;
        border-radius:20px;font-size:13px;font-weight:600;margin-left:12px}
  .pill .dot{width:8px;height:8px;border-radius:50%}
  .pill.idle{background:#1c3247;color:#8aa0b4}
  .pill.idle .dot{background:#8aa0b4}
  .pill.running{background:#0f3b30;color:#bff3e6}
  .pill.running .dot{background:#36c2a8;animation:pulse 1.4s infinite}
  .pill.error{background:#3b1212;color:#f3b0b0}
  .pill.error .dot{background:#e0555a}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:0.3}}
  .modal-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.6);
                 align-items:center;justify-content:center;z-index:100}
  .modal-overlay.show{display:flex}
  .modal-box{background:#fff;color:#1a1a1a;border-radius:12px;width:90%;max-width:560px;
             max-height:90vh;overflow:auto;box-shadow:0 10px 40px rgba(0,0,0,0.4)}
  .modal-head{display:flex;justify-content:space-between;align-items:center;
              padding:18px 22px;font-size:18px;font-weight:600;border-bottom:1px solid #eee}
  .modal-close{cursor:pointer;font-size:26px;color:#888;line-height:1}
  .modal-close:hover{color:#000}
  .modal-body{padding:18px 22px;text-align:center}
  .modal-body img{max-width:100%;border-radius:8px}
  .no-img{padding:50px 0;color:#999;font-size:15px;background:#f4f4f4;border-radius:8px}
  .modal-details{padding:0 22px 20px}
  .modal-detail-label{font-size:11px;letter-spacing:0.08em;color:#999;margin-bottom:4px}
  .modal-detail-value{font-size:15px;font-weight:600;color:#333}
</style></head>
<body>
  <header>
    <a class="back" href="{{ url_for('home') }}">&larr; Fleet</a>
    <h1>{{ dev['name'] or ('Device' ~ ' ' ~ dev['id']) }}<span class="pill idle" id="statusPill"><span class="dot"></span><span id="statusText">connecting…</span></span></h1>
    <a class="logout" href="{{ url_for('logout') }}">Logout</a>
  </header>
  <main>
    <div class="chart-card">
      <div class="nav">
        <button id="prevBtn" title="Older sample">&lsaquo;</button>
        <div class="label" id="navLabel">Loading…</div>
        <button id="nextBtn" title="Newer sample">&rsaquo;</button>
        <span class="newdata" id="newData">New data on live test &rarr;</span>
      </div>
      <p class="sub" id="sampleLabel"></p>
      <canvas id="curve" height="120"></canvas>
    </div>
    <div class="metrics" id="metrics"></div>
  </main>

  <div id="imgModal" class="modal-overlay" onclick="closeModal(event)">
    <div class="modal-box" onclick="event.stopPropagation()">
      <div class="modal-head">
        <span id="modalTitle">Event</span>
        <span class="modal-close" onclick="closeModal()">&times;</span>
      </div>
      <div class="modal-body">
        <img id="modalImg" src="" alt="" style="display:none">
        <div id="modalNoImg" class="no-img">No image available</div>
      </div>
      <div class="modal-details">
        <div class="modal-detail-label">DETAILS</div>
        <div id="modalDetail" class="modal-detail-value"></div>
      </div>
    </div>
  </div>

<script>
const deviceId = {{ dev['id'] }};
let chart = null;
let sampleList = [];      // newest first: [{sample_id, started_at, sv30, ...}, ...]
let idx = 0;              // which sample we are viewing (0 = newest)
let cache = {};           // sample_id -> payload (immutable old samples cached)
let liveSampleId = null;  // the sample_id currently receiving live data

function fmtTime(s){ if(!s) return ''; return s.replace('T',' ').slice(0,16); }

let currentEvents = [];   // events for the sample currently shown

function iconColor(type){
  if (type === 'error') return '#e0555a';      // red
  if (type === 'floating') return '#3a8fd0';   // blue
  return '#c98a3a';                             // amber for image marks (like ref)
}

function eventLabel(e){
  if (e.type === 'error') return '!';
  if (e.state === '30Mark') return '30';
  if (e.state === '60Mark') return '60';
  if (e.state === '90Mark') return '90';
  if (e.type === 'floating') return 'F';
  return '•';
}

// Custom plugin: draws labeled rounded-rectangle tags at each event point.
const eventTagPlugin = {
  id: 'eventTags',
  afterDatasetsDraw(chart){
    const ev = chart.$events || [];
    const xScale = chart.scales.x, yScale = chart.scales.y;
    const ctx = chart.ctx;
    chart.$tagBoxes = [];
    ev.forEach(e => {
      if (e.minute == null) return;
      const x = xScale.getPixelForValue(e.minute);
      const y = yScale.getPixelForValue(e._y ?? 50);
      const text = eventLabel(e);
      ctx.save();
      ctx.font = '600 12px system-ui,sans-serif';
      const tw = ctx.measureText(text).width;
      const padX = 7, h = 20, w = Math.max(tw + padX*2, 22);
      const bx = x - w/2, by = y - h - 8;   // tag sits just above the point
      // rounded rect
      const r = 5;
      ctx.beginPath();
      ctx.moveTo(bx+r, by);
      ctx.arcTo(bx+w, by, bx+w, by+h, r);
      ctx.arcTo(bx+w, by+h, bx, by+h, r);
      ctx.arcTo(bx, by+h, bx, by, r);
      ctx.arcTo(bx, by, bx+w, by, r);
      ctx.closePath();
      ctx.fillStyle = iconColor(e.type);
      ctx.fill();
      // little pointer line to the curve
      ctx.strokeStyle = iconColor(e.type);
      ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.moveTo(x, by+h); ctx.lineTo(x, y); ctx.stroke();
      // text
      ctx.fillStyle = '#fff';
      ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      ctx.fillText(text, x, by + h/2);
      ctx.restore();
      // store clickable box for hit detection
      chart.$tagBoxes.push({x1:bx, y1:by, x2:bx+w, y2:by+h, _event:e});
    });
  }
};

function ensureChart(points, events){
  events = events || [];
  currentEvents = events;
  const values = points.map(p => ({x: p.minute, y: p.value}));

  const valueAt = (m) => {
    const p = points.find(pt => pt.minute === m);
    return p ? p.value : null;
  };
  // attach the y-position each event should sit at
  const evForPlugin = events.filter(e => e.minute != null).map(e => {
    e._y = valueAt(e.minute) ?? 50;
    return e;
  });

  if (chart) chart.destroy();
  chart = new Chart(document.getElementById('curve'), {
    type:'line',
    data:{ datasets:[
      { label:'Sludge %', data:values, parsing:false,
        borderColor:'#e0a23a', backgroundColor:'rgba(224,162,58,0.15)',
        fill:true, tension:0.3, pointRadius:1, order:2 }
    ]},
    options:{
      onClick:(evt)=>{
        // hit-test the tag boxes drawn by the plugin
        const rect = chart.canvas.getBoundingClientRect();
        const mx = evt.native.clientX - rect.left;
        const my = evt.native.clientY - rect.top;
        for (const b of (chart.$tagBoxes||[])){
          if (mx>=b.x1 && mx<=b.x2 && my>=b.y1 && my<=b.y2){ openModal(b._event); return; }
        }
      },
      scales:{
        x:{ type:'linear', min:0, max:100, title:{display:true,text:'Minute'},
            ticks:{color:'#8aa0b4',stepSize:10}, grid:{color:'#1c3247'} },
        y:{ min:0, max:100, title:{display:true,text:'Sludge %'},
            ticks:{color:'#8aa0b4'}, grid:{color:'#1c3247'} }
      },
      plugins:{ legend:{ display:false } }
    },
    plugins:[eventTagPlugin]
  });
  chart.$events = evForPlugin;
  chart.update();
}

// ---- Modal ----
function openModal(e){
  document.getElementById('modalTitle').textContent =
    e.type === 'error' ? 'Error' :
    e.type === 'floating' ? 'Floating Sludge' :
    (e.state || 'Image');
  document.getElementById('modalDetail').textContent =
    (e.detail || '') + (e.captured_at ? ('  •  ' + e.captured_at.replace('T',' ').slice(0,19)) : '');
  const img = document.getElementById('modalImg');
  const noImg = document.getElementById('modalNoImg');
  if (e.image_url){
    img.src = e.image_url; img.style.display = 'block'; noImg.style.display = 'none';
  } else {
    img.style.display = 'none'; noImg.style.display = 'block';
  }
  document.getElementById('imgModal').classList.add('show');
}
function closeModal(ev){
  document.getElementById('imgModal').classList.remove('show');
}

async function fetchEvents(sample_id){
  try {
    const r = await fetch(`/api/device/${deviceId}/events/${sample_id}`);
    return await r.json();
  } catch(e){ return []; }
}

function renderMetrics(m){
  const box = document.getElementById('metrics');
  box.innerHTML = '';
  if (!m){ box.innerHTML = '<div class="metric"><div class="label">Test in progress</div><div class="value" style="font-size:15px">running…</div></div>'; return; }
  const items = [
    ['SV30', m.sv30!=null?m.sv30+'%':'—'],
    ['SV60', m.sv60!=null?m.sv60+'%':'—'],
    ['SV90', m.sv90!=null?m.sv90+'%':'—'],
    ['Initial velocity', m.initial_velocity!=null?m.initial_velocity:'—'],
    ['Compaction ratio', m.compaction_ratio!=null?m.compaction_ratio:'—'],
    ['Floating', m.floating_detected?('Yes (min '+m.floating_first_minute+')'):'No'],
  ];
  for (const [l,v] of items) box.innerHTML += `<div class="metric"><div class="label">${l}</div><div class="value">${v}</div></div>`;
  box.innerHTML += `<div class="metric"><div class="label">Settling class</div><div><span class="badge ${m.settling_class||'unknown'}">${m.settling_class||'unknown'}</span></div></div>`;
  box.innerHTML += `<div class="metric"><div class="label">Status</div><div class="value" style="font-size:15px">${m.status}</div></div>`;
}

async function fetchSample(sample_id){
  if (cache[sample_id]) return cache[sample_id];
  const res = await fetch(`/api/device/${deviceId}/sample/${sample_id}`);
  const data = await res.json();
  // Cache only completed (immutable) samples; never cache the live one.
  if (sample_id !== liveSampleId && data.metrics && data.metrics.status === 'complete')
    cache[sample_id] = data;
  return data;
}

async function showIndex(i){
  if (sampleList.length === 0) return;
  idx = Math.max(0, Math.min(i, sampleList.length - 1));
  const entry = sampleList[idx];
  const data = await fetchSample(entry.sample_id);
  const events = await fetchEvents(entry.sample_id);
  ensureChart(data.points, events);
  renderMetrics(data.metrics);
  document.getElementById('navLabel').textContent =
    `Sample ${sampleList.length - idx} of ${sampleList.length}  (${fmtTime(entry.started_at)})`;
  document.getElementById('sampleLabel').textContent = 'Sample ' + entry.sample_id;
  document.getElementById('prevBtn').disabled = (idx >= sampleList.length - 1);
  document.getElementById('nextBtn').disabled = (idx <= 0);
  if (idx === 0) document.getElementById('newData').style.display = 'none';
}

let viewingLive = false;   // true when the chart is showing the running test

async function showLive(){
  // Fetch the current running test from /latest (reads raw readings, not the
  // completed-samples table, so it works for an in-progress test).
  const r = await fetch(`/api/device/${deviceId}/latest`);
  const d = await r.json();
  if (!d.sample_id){ return false; }
  liveSampleId = d.sample_id;
  viewingLive = true;
  idx = -1;   // -1 = live view (not a completed-list index)
  const events = await fetchEvents(d.sample_id);
  ensureChart(d.points, events);
  renderMetrics(d.metrics);
  document.getElementById('navLabel').textContent =
    'Live test — ' + (d.points.length ? ('minute ' + d.points[d.points.length-1].minute) : 'starting…');
  document.getElementById('sampleLabel').textContent = 'Sample ' + d.sample_id + '  (running)';
  document.getElementById('nextBtn').disabled = true;   // nothing newer than live
  document.getElementById('prevBtn').disabled = (sampleList.length === 0);
  document.getElementById('newData').style.display = 'none';
  return true;
}

async function loadList(){
  const res = await fetch(`/api/device/${deviceId}/samples`);
  sampleList = await res.json();

  // Check if a test is ACTUALLY running right now (device state, not just
  // "there's a recent sample"). The /latest endpoint reports is_running.
  const r = await fetch(`/api/device/${deviceId}/latest`);
  const d = await r.json();

  if (d.is_running && d.sample_id){
    await showLive();
    return;
  }

  // No running test — show the newest completed sample (or empty).
  viewingLive = false;
  if (sampleList.length === 0){
    document.getElementById('navLabel').textContent = 'No samples yet';
    document.getElementById('prevBtn').disabled = true;
    document.getElementById('nextBtn').disabled = true;
    return;
  }
  liveSampleId = sampleList[0].sample_id;
  showIndex(0);
}

document.getElementById('prevBtn').onclick = () => {
  // From live view, prev goes to the newest completed sample.
  if (viewingLive){ viewingLive = false; showIndex(0); }
  else showIndex(idx + 1);
};
document.getElementById('nextBtn').onclick = () => {
  if (!viewingLive) showIndex(idx - 1);
};
document.getElementById('newData').onclick = () => { showLive(); };

loadList();

// ---- Live status pill + live updates ----
function setPill(state){
  const pill = document.getElementById('statusPill');
  const text = document.getElementById('statusText');
  const running = ['start','ongoing','30Mark','60Mark','90Mark'].includes(state);
  if (state === 'error'){ pill.className='pill error'; text.textContent='Error'; }
  else if (running){ pill.className='pill running'; text.textContent='Test running'; }
  else { pill.className='pill idle'; text.textContent='Idle'; }
}

const evtSource = new EventSource(`/stream/device/${deviceId}`);
evtSource.onmessage = function(e){
  const d = JSON.parse(e.data);
  setPill(d.state);

  const runningStates = ['start','ongoing','30Mark','60Mark','90Mark','floatingSludge'];
  const isRunning = runningStates.includes(d.state);

  // If the device is idle/ended (no active test) but we're still showing a
  // live view, the test just finished — drop out of live and show history.
  if (!isRunning && viewingLive){
    viewingLive = false;
    loadList();   // will now show the newest completed sample
    return;
  }

  if (!d.sample_id || d.value === null || d.minute === null) return;

  // A new/different running sample appeared, and we're not already showing it.
  if (isRunning && d.sample_id !== liveSampleId){
    showLive();
    return;
  }

  // Live data for the current running test. If the user is browsing an older
  // completed sample (not live view), don't disrupt — show the nudge.
  if (!viewingLive){
    document.getElementById('newData').style.display = 'inline-block';
    return;
  }

  // We're in live view — append/update the point (x,y format).
  if (!chart) return;
  const arr = chart.data.datasets[0].data;
  const pos = arr.findIndex(pt => pt.x === d.minute);
  if (pos === -1){ arr.push({x: d.minute, y: d.value}); }
  else { arr[pos].y = d.value; }
  chart.update('none');
  document.getElementById('navLabel').textContent = 'Live test — minute ' + d.minute;
  document.getElementById('sampleLabel').textContent =
    'Sample ' + d.sample_id + '  —  live (minute ' + d.minute + ', state ' + d.state + ')';
};
evtSource.onerror = function(){ document.getElementById('statusText').textContent = 'reconnecting…'; };
</script>
</body></html>"""


_latest_state = {}          # device_id -> {"last_id": int, "rows": [recent rows]}
_state_lock = threading.Lock()


def _watch_db():
    """
    Single background thread. Once per second, checks the DB for new readings
    and updates the shared _latest_state. All SSE streams read from this,
    so connections don't each hit the database.
    """
    conn = db.get_connection()
    while True:
        try:
            # For each device, get its most recent reading id + recent points.
            devices = conn.execute("SELECT id FROM devices").fetchall()
            with _state_lock:
                for d in devices:
                    did = d["id"]
                    row = conn.execute(
                        """SELECT id, sample_id, minute, state, sludge_value
                           FROM readings WHERE device_id = ?
                           ORDER BY id DESC LIMIT 1""", (did,)).fetchone()
                    if row:
                        _latest_state[did] = {
                            "last_id": row["id"],
                            "sample_id": row["sample_id"],
                            "minute": row["minute"],
                            "state": row["state"],
                            "value": row["sludge_value"],
                        }
        except Exception as e:
            print(f"watch_db error: {e}")
        time.sleep(1)


# Start the single watcher thread when the app starts.
_watcher = threading.Thread(target=_watch_db, daemon=True)
_watcher.start()


@app.route("/stream/device/<int:device_id>")
@login_required
def stream_device(device_id):
    """
    SSE endpoint. Holds one persistent connection to the browser and pushes
    a message whenever this device has a NEW reading. Reads only from the
    shared in-memory state (no per-connection DB queries).
    """
    user = get_current_user()
    conn = db.get_connection()
    allowed = device_allowed(conn, user, device_id)
    conn.close()
    if not allowed:
        return "forbidden", 403

    @stream_with_context
    def event_stream():
        last_sent_id = None
        heartbeat = 0
        while True:
            with _state_lock:
                state = _latest_state.get(device_id)
            if state and state["last_id"] != last_sent_id:
                last_sent_id = state["last_id"]
                # Push only the new point (delta), as a JSON SSE event.
                payload = json.dumps({
                    "sample_id": state["sample_id"],
                    "minute": state["minute"],
                    "state": state["state"],
                    "value": state["value"],
                })
                yield f"data: {payload}\n\n"
            else:
                # Heartbeat every ~15s keeps the connection alive and lets
                # dead connections be detected/closed (frees the thread).
                heartbeat += 1
                if heartbeat >= 15:
                    heartbeat = 0
                    yield ": keepalive\n\n"
            time.sleep(1)

    return Response(event_stream(), mimetype="text/event-stream")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, threaded=True)
