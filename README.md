# FlocDetector — Edge-to-Cloud IoT Monitoring for Wastewater Treatment

A full-stack IoT platform that monitors activated-sludge settling (the SV30 test)
across a fleet of field sensors, streams live settling curves to a web dashboard,
and captures snapshot images at key moments — all running on a single, deliberately
under-resourced cloud instance.

> **The engineering theme of this project:** building a complete, reliable system
> under hard hardware constraints (a shared **2 GB RAM** box), where every
> architectural decision was driven by what the hardware could actually sustain.

---

## What it does

Activated-sludge plants run a **settling test** — sludge settles in a beaker and
the settled volume after 30 minutes (SV30) indicates the health of the biology.
FlocDetector automates this: a camera + ML model on a Raspberry Pi watches the
beaker, measures the sludge interface every minute, and reports it. This platform:

- **Ingests** per-minute telemetry from many field units over MQTT (AWS IoT Core)
- **Computes** per-test metrics (SV30/60/90, settling class: healthy / slow / bulking / rising)
- **Streams** live settling curves to a browser in real time (Server-Sent Events)
- **Captures** snapshot images at 30/60/90 min, on floating sludge, and on errors —
  stored in S3, shown as clickable tags on the curve
- **Serves** multiple users with role-based access and per-device (plant-local) timezones

---

## Architecture

```
  FIELD UNIT (Raspberry Pi)                  CLOUD (single 2 GB EC2)
  ┌────────────────────────┐                  ┌────────────────────────────────┐
  │  Camera → CV model     │  MQTT / TLS      │   ingest.py ──► SQLite         │
  │  main.py ──────────────┼───────────────── ┼──► (telemetry)   flocdash.db   │
  │  Node-RED              │  HTTP multipart  │   image_service.py ──► S3      │
  │      └─────────────────┼───────────────── ┼──► (snapshots)    ▲            │
  │                        │                  │   app.py (Flask) ─┘  dashboard │
  └────────────────────────┘                  │   + SSE live updates  :5000    │
                              browser ◄───────┼─── presigned S3 image URLs     │
                                              └───────────────────────────────┘
```

**Three decoupled Python services**, each ~30 MB, sharing one SQLite database:

| Service | Role | Why it's separate |
|---|---|---|
| `ingest.py` | MQTT subscriber → normalizes & stores telemetry | Can run without the UI |
| `app.py` | Flask dashboard, live SSE, presigned image URLs | User-facing, isolated |
| `image_service.py` | Receives images → S3 → DB reference | Image spikes don't touch the dashboard |

---

## Key engineering decisions (and the constraints behind them)

This project is really a case study in **matching architecture to hardware**.

| Decision | Why | The constraint it respected |
|---|---|---|
| **SQLite**, not Postgres/TimescaleDB | Zero idle memory; it's just a file | Postgres would eat hundreds of MB the box didn't have |
| **Three plain-Flask processes**, not one FastAPI | 3 × ~30 MB fits; FastAPI+uvicorn's startup spike triggered the OOM killer | ~1.2 GB usable RAM, shared with other production services |
| **Direct MQTT subscribe**, not the existing SQS pipeline | Portability — points at any MQTT broker, no vendor lock-in | Building a generalizable product, not AWS-coupled glue |
| **Precomputed metrics** at test-close | Read-heavy dashboard shouldn't recompute on every load | Keep per-request work minimal |
| **S3 for images, keys in the DB** | Databases handle blobs poorly; S3 is built for them | Keep the DB small and fast |
| **Lazy vs. eager library loading** | boto3's import spike was placed where it hurt least (startup in one service, first-use in another) | Avoid request-time OOM kills |
| **Store UTC, convert per-device at display** | Units span multiple countries; each shows plant-local time | Unambiguous storage, correct multi-timezone display |

The recurring lesson: **a "better" tool that doesn't fit is worse than a simpler
one that runs reliably.**

---

## Notable features

- **Live settling curves** via Server-Sent Events — a single shared DB-watcher
  thread feeds all connected browsers, so N viewers ≠ N database queries
- **Resilient sample lifecycle** — four independent triggers close a test
  (explicit end, full duration, new-sample-started, and a time-based sweep for
  units that drop mid-test), plus a validity gate that discards tests under 30 min
- **Idempotent ingestion** — a UNIQUE constraint + caught IntegrityError makes
  MQTT redelivery safe (no duplicate readings)
- **Interactive event tags** — a custom Chart.js plugin draws labeled tags
  (30 / 60 / 90 / F / !) on the curve; click to view the S3 image in a modal
- **Role-based access** — admins see the whole fleet; users see only assigned units
- **Per-device timezones** — each unit's timestamps display in its own local time

---

## Tech stack

**Edge:** Raspberry Pi · Python · YOLO (Ultralytics) · Node-RED · MQTT
**Cloud:** AWS EC2 · AWS IoT Core · Amazon S3 · SQLite · Python · Flask · paho-mqtt · boto3
**Frontend:** Server-Sent Events · Chart.js (custom plugin)
**Ops:** PM2 (process management, auto-restart, boot persistence)

---

## Repository structure

```
.
├── cloud/
│   ├── db.py                 # data-access layer: storage, metrics, sample lifecycle
│   ├── ingest.py             # MQTT ingestion service
│   ├── app.py                # Flask dashboard: pages, APIs, SSE, presigned URLs
│   ├── image_service.py      # image upload → S3 service
│   ├── schema.sql            # database schema (7 tables)
│   ├── create_user.py        # helper: add dashboard users
│   ├── migrate_*.py          # incremental schema migrations
│   └── start_*.sh            # PM2 wrapper scripts
├── edge/
│   └── main.py               # Raspberry Pi capture + inference + MQTT publish
├── docs/
│   └── implementation-guide.md   # full step-by-step build guide (the "how & why")
└── README.md
```

> **Note on secrets:** no credentials are committed. All secrets (AWS keys, tokens,
> device certificates) load from a `.env.s3` file and a `certs/` directory that are
> git-ignored. See `.env.example` for the expected shape.

---

## Documentation

The [`docs/implementation-guide.md`](docs/implementation-guide.md) is a complete,
teach-yourself walkthrough: schema design with per-column rationale, the ingestion
and metric-computation logic, the SSE scaling pattern, the S3 image pipeline, PM2
deployment gotchas, and a troubleshooting log of every real bug encountered
(OOM spikes, PM2's interpreter confusion, phantom "running" states, timezone
normalization).

---

## Status

Running in production, monitoring a live fleet. Built incrementally — each piece
tested against real device data before moving on.

### Possible future work
- HTTPS via reverse proxy · gunicorn (production WSGI) · trends & alerting
  (rising SV30, bulking, offline) · automated DB backups
