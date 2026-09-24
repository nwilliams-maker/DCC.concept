-- DCC database schema
--
-- Replaces the Google Sheet backend (IC_SHEET_URL tabs) and the GAS Script
-- Properties store (bundle maps) with a real Postgres schema. Run once
-- against a fresh database (e.g. Railway's Postgres add-on):
--
--   psql "$DATABASE_URL" -f migration/schema.sql
--
-- See migration/README.md for the full migration sequence and
-- migration/import_from_sheets.py for the one-time data import that follows.

BEGIN;

CREATE TYPE route_status AS ENUM ('sent', 'accepted', 'declined', 'finalized', 'archived');
CREATE TYPE fn_status AS ENUM ('posted', 'assigned');

-- Replaces the Contractors_DB tab (gid=0). Read-only in the app today;
-- `unrestricted` replaces the hardcoded _UNRESTRICTED_ICS name list in
-- tactical_workspace_master_rw.py.
CREATE TABLE contractors (
    id                 BIGSERIAL PRIMARY KEY,
    email              TEXT NOT NULL UNIQUE,
    name               TEXT NOT NULL,
    location           TEXT,
    phone              TEXT,
    ic_list            TEXT,
    lat                DOUBLE PRECISION,
    lng                DOUBLE PRECISION,
    pod_color          TEXT,
    digital_certified  BOOLEAN NOT NULL DEFAULT FALSE,
    unrestricted       BOOLEAN NOT NULL DEFAULT FALSE,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Replaces the Saved/Accepted/Declined/Finalized route tabs (one row schema,
-- distinguished only by which tab it lived in -> now `status`).
-- `payload` keeps the full original JSON payload verbatim so nothing is lost
-- for fields not promoted to their own column; `locs`/`stop_data`/`comp`/`due`/
-- `cluster_hash` are pulled out because the app reads them often.
CREATE TABLE routes (
    id                 BIGSERIAL PRIMARY KEY,
    wo                 TEXT NOT NULL UNIQUE,
    contractor_id      BIGINT REFERENCES contractors(id),
    contractor_name    TEXT NOT NULL,
    status             route_status NOT NULL,
    comp               NUMERIC,
    due                DATE,
    locs               JSONB,
    stop_data          JSONB,
    cluster_hash       TEXT,
    payload            JSONB NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_routes_status ON routes (status);
CREATE INDEX idx_routes_cluster_hash ON routes (cluster_hash);
CREATE INDEX idx_routes_contractor ON routes (contractor_id);

-- Contractor emails sent before the Railway cutover contain R-... route IDs.
-- Keep each old link distinct, including archived (inactive) links.
CREATE TABLE legacy_route_links (
    route_id           TEXT PRIMARY KEY,
    wo                 TEXT NOT NULL,
    payload            JSONB NOT NULL,
    source_status      TEXT NOT NULL,
    active             BOOLEAN NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL
);

-- Replaces the Field Nation tab (gid=1396320527). Separate from `routes`
-- because every row's "contractor" is the vendor itself, not an IC, and it
-- has its own two-state lifecycle (Posted -> Assigned).
CREATE TABLE field_nation_orders (
    id                 BIGSERIAL PRIMARY KEY,
    work_order         TEXT NOT NULL UNIQUE,
    status             fn_status NOT NULL DEFAULT 'posted',
    provider           TEXT,
    route_plan_id      TEXT,
    payload            JSONB NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_fn_orders_status ON field_nation_orders (status);

-- Replaces the Archive tab AND the GAS getSyncVersion change log with one
-- append-only ledger. `id` doubles as the monotonic version number the app's
-- incremental "what changed since version N" poll (fetch_sync_status) reads
-- from -- no separate version column needed.
CREATE TABLE route_events (
    id                 BIGSERIAL PRIMARY KEY,
    route_id           BIGINT REFERENCES routes(id),
    action             TEXT NOT NULL, -- archiveRoute / finalizeRoute / processDecision / saveRoute / ...
    payload            JSONB,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_route_events_route ON route_events (route_id);

-- Replaces GAS Script Properties (saveBundleMap / loadBundleMap) -- the
-- dispatcher's in-progress route-bundling state per pod.
CREATE TABLE bundle_maps (
    id                 BIGSERIAL PRIMARY KEY,
    pod                TEXT NOT NULL,
    dispatcher_id      TEXT NOT NULL,
    task_id_sets       JSONB NOT NULL,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (pod, dispatcher_id)
);

-- Replaces scanning the Archive tab for the highest-used WO suffix per
-- contractor/day -- a real counter instead of a table scan.
CREATE TABLE wo_counters (
    contractor_name    TEXT NOT NULL,
    wo_date            DATE NOT NULL,
    next_suffix        INT NOT NULL DEFAULT 1,
    PRIMARY KEY (contractor_name, wo_date)
);

COMMIT;
