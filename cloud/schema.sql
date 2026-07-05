-- ============================================================================
--  SV30 / FlocDetector Cloud Dashboard — Database Schema (Stage 1)
--  PostgreSQL + TimescaleDB
-- ============================================================================
--  Design notes for learning:
--   * A "device" is the pair (plant_id, plc_id) — neither is unique alone.
--   * The client-specific tag name (SVOL_1:PAR_PAK / VAT_VAT / EMS_EMS) is
--     NORMALIZED on ingestion into a neutral column `sludge_value`. The DB
--     never stores the wire-format tag name — that keeps the schema reusable
--     across different clients.
--   * A "sample" = one full settling test (grouped by sample_id).
--   * "readings" = the per-minute time-series stream (a TimescaleDB hypertable).
--   * "samples"  = one row per completed test, holding PRE-COMPUTED metrics
--     (SV30 etc.) so the dashboard never recomputes from raw rows on page load.
-- ============================================================================


-- Enable the TimescaleDB extension (adds time-series superpowers to Postgres).
-- Safe to run repeatedly.
CREATE EXTENSION IF NOT EXISTS timescaledb;


-- ============================================================================
--  1. DEVICES  — one row per physical FlocDetector unit
-- ============================================================================
--  Why: incoming payloads identify a unit by (plant_id, plc_id). We give each
--  unit its own internal integer id (device_id) so every other table can refer
--  to it with a small, fast key instead of repeating two long Mongo-style ids.
CREATE TABLE IF NOT EXISTS devices (
    id           SERIAL PRIMARY KEY,           -- internal id, used everywhere else
    plant_id     TEXT NOT NULL,                -- from payload.plantId
    plc_id       TEXT NOT NULL,                -- from payload.plcId
    name         TEXT,                         -- friendly name e.g. "Vatika STP - Aeration 1"
    location     TEXT,                         -- optional site/location label
    first_seen   TIMESTAMPTZ DEFAULT now(),    -- when we first received data from it
    last_seen    TIMESTAMPTZ,                  -- updated on every reading (health/online check)

    -- A unit is uniquely the COMBINATION of plant + plc. This constraint means
    -- the same pair can never be inserted twice, and lets ingestion do a fast
    -- "get or create" lookup.
    UNIQUE (plant_id, plc_id)
);


-- ============================================================================
--  2. SAMPLES  — one row per test run (the settling test)
-- ============================================================================
--  Why separate from readings: a "sample" is the natural unit an operator cares
--  about ("show me the 2:31 PM test"). We store the DERIVED METRICS here,
--  computed ONCE when the test finishes, so the dashboard reads one row instead
--  of scanning ~100 raw readings every time. This is the single biggest
--  performance decision in the whole design.
CREATE TABLE IF NOT EXISTS samples (
    id             SERIAL PRIMARY KEY,
    device_id      INTEGER NOT NULL REFERENCES devices(id),
    sample_id      TEXT NOT NULL,              -- the payload.sampleId (device-generated)
    started_at     TIMESTAMPTZ,                -- time of first reading in this test
    ended_at       TIMESTAMPTZ,                -- time of the 'end' reading
    status         TEXT DEFAULT 'running',     -- 'running' | 'complete' | 'aborted'

    -- ---- Derived metrics (filled in when the test completes) ----
    sv5            REAL,                        -- settled value at ~5 min
    sv30           REAL,                        -- settled value at 30 min (headline number)
    sv60           REAL,                        -- settled value at 60 min
    sv90           REAL,                        -- settled value at 90 min (final plateau)
    initial_velocity   REAL,                    -- settling speed, early slope (%/min)
    compaction_ratio   REAL,                    -- sv30 / sv90 (how much more it compacts)
    settling_class     TEXT,                    -- 'healthy' | 'slow' | 'bulking' | 'rising'

    -- ---- Floating sludge event info ----
    floating_detected     BOOLEAN DEFAULT FALSE,
    floating_first_minute INTEGER,              -- minute when floating first seen
    floating_first_at     TIMESTAMPTZ,          -- timestamp of first floating detection

    created_at     TIMESTAMPTZ DEFAULT now(),

    -- The same device cannot have two tests with the same sample_id. Lets
    -- ingestion do "get or create" for the sample a reading belongs to.
    UNIQUE (device_id, sample_id)
);

-- Index: the dashboard constantly asks "latest samples for this device,
-- newest first". This index makes that query instant.
CREATE INDEX IF NOT EXISTS idx_samples_device_time
    ON samples (device_id, started_at DESC);


-- ============================================================================
--  3. READINGS  — the per-minute time-series stream (TimescaleDB hypertable)
-- ============================================================================
--  Why a hypertable: this is the table that grows forever (every minute, every
--  unit). Timescale automatically splits it into time-based "chunks" so queries
--  like "this test" or "last 24h" only touch the relevant chunk, staying fast
--  as the table grows to millions of rows.
CREATE TABLE IF NOT EXISTS readings (
    device_id     INTEGER NOT NULL REFERENCES devices(id),
    ts            TIMESTAMPTZ NOT NULL,         -- the reading's timestamp (time dimension)
    sample_id     TEXT,                         -- which test (NULL when idle/ended)
    minute        INTEGER,                      -- minute index within the test (0..91)
    state         TEXT,                         -- idle/start/ongoing/30Mark/.../end/floatingSludge
    sludge_value  REAL,                         -- NORMALIZED raw sludge reading (from SVOL_1:*)
    floating      BOOLEAN DEFAULT FALSE,        -- true on a floatingSludge reading

    -- Note: no PRIMARY KEY here in the classic sense — hypertables key on time.
    -- We enforce de-duplication with the unique index below instead.

    -- This is a design choice: `ts` is the real time dimension so that if you
    -- ever move to per-second data, the same table works — you just insert at
    -- finer granularity. `minute` stays as a convenience field.
    UNIQUE (device_id, sample_id, minute, state)
);

-- Turn `readings` into a Timescale hypertable, partitioned on `ts`.
-- (if_not_exists so re-running is safe; migrate_data handles an existing table)
SELECT create_hypertable('readings', 'ts',
                         if_not_exists => TRUE,
                         migrate_data  => TRUE);

-- Index: "give me all readings for this test, in time order" — the query the
-- single-unit curve page runs every time.
CREATE INDEX IF NOT EXISTS idx_readings_sample
    ON readings (device_id, sample_id, ts);


-- ============================================================================
--  4. ERRORS  — device error events (separate payload shape, no sampleId/minute)
-- ============================================================================
--  Why separate table: error payloads have a different shape (error_code +
--  message, no sampleId, no minute). Mixing them into `readings` would leave
--  most columns NULL and muddy queries. A dedicated table is cleaner.
CREATE TABLE IF NOT EXISTS device_errors (
    id           SERIAL PRIMARY KEY,
    device_id    INTEGER NOT NULL REFERENCES devices(id),
    ts           TIMESTAMPTZ NOT NULL,
    error_code   TEXT,                          -- E001..E007
    message      TEXT,
    created_at   TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_errors_device_time
    ON device_errors (device_id, ts DESC);


-- ============================================================================
--  5. IMAGES  — reference rows only; actual bytes live in S3
-- ============================================================================
--  Why: NEVER store image bytes in the DB. The image service uploads the file
--  to S3 and writes ONE small row here holding the S3 key + metadata. The web
--  app builds a presigned URL from s3_key when a user views the sample.
CREATE TABLE IF NOT EXISTS images (
    id           SERIAL PRIMARY KEY,
    device_id    INTEGER NOT NULL REFERENCES devices(id),
    sample_id    TEXT,                          -- which test this image belongs to
    state        TEXT,                          -- '30Mark' | '60Mark' | '90Mark' | 'floatingSludge'
    s3_key       TEXT NOT NULL,                 -- e.g. flocdetector/<plant>/<sample>/30min.jpg
    coordinates  JSONB,                         -- imageCoordinates bounding boxes
    captured_at  TIMESTAMPTZ,
    created_at   TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_images_sample
    ON images (device_id, sample_id);


-- ============================================================================
--  6. USERS  — login accounts
-- ============================================================================
CREATE TABLE IF NOT EXISTS users (
    id             SERIAL PRIMARY KEY,
    username       TEXT NOT NULL UNIQUE,
    password_hash  TEXT NOT NULL,               -- bcrypt/werkzeug hash, NEVER plaintext
    role           TEXT NOT NULL DEFAULT 'user',-- 'admin' (sees all) | 'user' (assigned only)
    created_at     TIMESTAMPTZ DEFAULT now()
);


-- ============================================================================
--  7. USER_DEVICES  — which user can see which device (many-to-many)
-- ============================================================================
--  Why: a regular user sees only their assigned devices. An admin sees all
--  (enforced in app code, so admins don't need rows here). This table maps
--  the "user X can view device Y" relationships.
CREATE TABLE IF NOT EXISTS user_devices (
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    device_id  INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, device_id)            -- each pair only once
);
