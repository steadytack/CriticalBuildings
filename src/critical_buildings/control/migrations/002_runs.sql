-- Control store schema, version 2: run lifecycle (runs, releases, per-run change history).

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

-- What changed in each run versus the one before it: cheap history ("when did this building appear / change type?")
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
