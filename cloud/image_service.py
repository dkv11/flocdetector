"""
image_service.py — Lightweight standalone Flask service for image uploads.
Runs as its own process on port 5001, separate from the dashboard.

Receives images (multipart form-data), uploads to S3, records a row in the
images table. Reads AWS credentials from .env.s3.

POST /upload   fields: file, plantId, plcId, sampleId, state, timestamp
GET  /health   simple check
"""

import os
import sqlite3
from datetime import datetime
from flask import Flask, request, jsonify

DB_FILE = "/home/ubuntu/flocdashboard/flocdash.db"
ENV_FILE = "/home/ubuntu/flocdashboard/.env.s3"

# ---- load S3 config ----
_cfg = {}
for _line in open(ENV_FILE):
    if "=" in _line and not _line.strip().startswith("#"):
        _k, _v = _line.strip().split("=", 1)
        _cfg[_k] = _v

S3_BUCKET = _cfg["S3_BUCKET"]
UPLOAD_TOKEN = _cfg.get("UPLOAD_TOKEN")   # shared secret; uploads must present it

# Create the boto3 S3 client at STARTUP (not lazily on first request).
# Loading boto3 causes a one-time memory spike; doing it at boot — when nothing
# else is spiking — avoids that spike happening during an upload request, which
# was getting the request OOM-killed on this small box.
import boto3
_s3 = boto3.client(
    "s3",
    aws_access_key_id=_cfg["AWS_ACCESS_KEY_ID"],
    aws_secret_access_key=_cfg["AWS_SECRET_ACCESS_KEY"],
    region_name=_cfg["AWS_REGION"],
)

def get_s3():
    return _s3

app = Flask(__name__)


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/upload", methods=["POST"])
def upload():
    # --- Auth: require the shared secret token ---
    # Accept it either as a header (X-Upload-Token) or a form field (token).
    if UPLOAD_TOKEN:
        provided = request.headers.get("X-Upload-Token") or request.form.get("token")
        if provided != UPLOAD_TOKEN:
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
    row = conn.execute("SELECT id FROM devices WHERE plant_id = ? AND plc_id = ?",
                       (plant_id, plc_id)).fetchone()
    if not row:
        conn.close()
        return jsonify({"ok": False, "error": "device not found"}), 404
    device_id = row[0]

    safe_state = (state or "image").replace("/", "_")
    safe_sample = sample_id or "no_sample"
    s3_key = f"flocdetector/{plant_id}/{safe_sample}/{safe_state}.jpg"

    try:
        get_s3().put_object(
            Bucket=S3_BUCKET, Key=s3_key,
            Body=f.read(), ContentType="image/jpeg")
    except Exception as e:
        conn.close()
        return jsonify({"ok": False, "error": f"s3 upload failed: {e}"}), 500

    # Store captured_at in UTC (server clock is UTC). We intentionally ignore
    # the device's `timestamp` field here because it arrives in device-local
    # time with an inconsistent format; images reach us within seconds of
    # capture, so the server's UTC receipt time is accurate and — crucially —
    # keeps ALL timestamps in the DB uniformly UTC, so display-time timezone
    # conversion works correctly.
    now = datetime.utcnow().isoformat()
    conn.execute(
        """INSERT INTO images (device_id, sample_id, state, s3_key, captured_at)
           VALUES (?, ?, ?, ?, ?)""",
        (device_id, sample_id, state, s3_key, now))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "s3_key": s3_key, "device_id": device_id})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001)
