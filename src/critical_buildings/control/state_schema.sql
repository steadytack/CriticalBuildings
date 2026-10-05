-- Operational state, version 1: everything the pipeline records about what it has done (data/state.db, not in git).
-- Kept apart from governed/control.db so that building a dataset never changes the governed, reviewed rules store.
-- Column order matches the tables as they were in control.db, so a legacy store is copied across as it is.

-- Append-only identity registries: IDs are never reused or deleted, only retired
CREATE TABLE building_registry (
    building_id    TEXT PRIMARY KEY,
    os_key         TEXT,
    osm_key        TEXT,
    created        TEXT NOT NULL,
    wkt            TEXT,
    first_seen_run TEXT NOT NULL,
    last_seen_run  TEXT NOT NULL,
    retired_run    TEXT,
    site_id        TEXT                                     -- the site the building was last assigned to
);
CREATE TABLE site_registry (
    site_id        TEXT PRIMARY KEY,
    created        TEXT NOT NULL,
    first_seen_run TEXT NOT NULL,
    last_seen_run  TEXT NOT NULL,
    retired_run    TEXT
);
CREATE INDEX idx_building_registry_os ON building_registry (os_key);
CREATE INDEX idx_building_registry_osm ON building_registry (osm_key);

-- Runs, releases and per-run change history
CREATE TABLE run (
    run_id                 TEXT PRIMARY KEY,                -- e.g. 20261001T120000_suffolk
    area                   TEXT NOT NULL,
    run_type               TEXT NOT NULL CHECK (run_type IN ('dev', 'candidate', 'release')),
    status                 TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed', 'archived')),
    started_at             TEXT NOT NULL,
    finished_at            TEXT,
    started_by             TEXT NOT NULL DEFAULT '',
    rule_version_id        INTEGER,
    rule_version_label     TEXT,
    config_hash            TEXT,
    bronze_key             TEXT,                            -- hash of the exact bronze inputs
    bronze_snapshots_json  TEXT,                            -- [{source, snapshot}] used by this run
    silver_key             TEXT,                            -- silver build the run used (or created)
    git_commit             TEXT,
    buildings              INTEGER,
    sites                  INTEGER,
    output_hash            TEXT,                            -- canonical content hash of the buildings table
    previous_run_id        TEXT,
    notes                  TEXT NOT NULL DEFAULT '',
    error                  TEXT,
    archive_path           TEXT
);

CREATE TABLE release (
    release     TEXT PRIMARY KEY,                           -- e.g. v2026.10.0
    area        TEXT NOT NULL,
    run_id      TEXT NOT NULL REFERENCES run (run_id),
    released_at TEXT NOT NULL,
    released_by TEXT NOT NULL,
    notes       TEXT NOT NULL DEFAULT ''
);

CREATE TABLE run_change (
    run_id            TEXT NOT NULL REFERENCES run (run_id),
    building_id       TEXT NOT NULL,
    change            TEXT NOT NULL,
    before_value      TEXT,
    after_value       TEXT,
    source_feature_id TEXT
);
CREATE INDEX idx_run_change_run ON run_change (run_id);
CREATE INDEX idx_run_change_building ON run_change (building_id);

CREATE TABLE run_stage (
    run_id        TEXT NOT NULL REFERENCES run(run_id),
    stage         TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
    started_at    TEXT,
    finished_at   TEXT,
    detail_json   TEXT NOT NULL DEFAULT '{}',               -- counts and notes the stage recorded, plus seconds
    PRIMARY KEY (run_id, stage)
);
