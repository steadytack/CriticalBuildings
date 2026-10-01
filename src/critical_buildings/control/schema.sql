-- Control store schema, version 1.
-- The governed store: business rules (draft + immutable published versions), audit trail, human decisions,
-- and the append-only identity registries. Edit through the UI / store layer, not by hand.

CREATE TABLE rule (
    rule_ref          INTEGER PRIMARY KEY,
    primary_category  TEXT    NOT NULL,
    building_type     TEXT    NOT NULL,
    importance        TEXT    NOT NULL CHECK (importance IN ('High', 'Medium', 'Low')),
    importance_driver TEXT    NOT NULL DEFAULT '',
    priority          INTEGER NOT NULL DEFAULT 50,          -- lower number wins when a feature matches several rules
    active            INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    notes             TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE rule_tag (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_ref INTEGER NOT NULL REFERENCES rule (rule_ref) ON DELETE CASCADE,
    source   TEXT    NOT NULL CHECK (source IN ('OSM', 'OS', 'MANUAL')),
    tag      TEXT    NOT NULL DEFAULT '',                    -- OSM: key=value (value * = any). OS: Theme > Classification
    active   INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    notes    TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE role_setting (key TEXT PRIMARY KEY, value TEXT NOT NULL);          -- min_area
CREATE TABLE role_osm_value (
    kind  TEXT NOT NULL CHECK (kind IN ('ancillary', 'primary')),
    value TEXT NOT NULL,
    PRIMARY KEY (kind, value)
);
CREATE TABLE role_type_min (building_type TEXT PRIMARY KEY, min_area REAL NOT NULL);

CREATE TABLE reference_mapping (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    sort_order    INTEGER NOT NULL,                          -- first match wins, top to bottom within a source
    source        TEXT    NOT NULL CHECK (source IN ('DfE', 'CQC', 'NHS ODS')),
    field         TEXT    NOT NULL,
    match         TEXT    NOT NULL CHECK (match IN ('Contains', 'Equals')),
    value         TEXT    NOT NULL,
    building_type TEXT    NOT NULL,                          -- a Building Type, or IGNORE
    active        INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    notes         TEXT    NOT NULL DEFAULT ''
);

-- Immutable published rule sets. Runs record the version id they used, so any run can be reproduced.
CREATE TABLE rule_set_version (
    version_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL,
    created_by    TEXT NOT NULL,
    approved_by   TEXT NOT NULL DEFAULT '',
    reason        TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    snapshot_json TEXT NOT NULL
);

-- Audit trail of every edit to the governed tables
CREATE TABLE change_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    user       TEXT NOT NULL,
    table_name TEXT NOT NULL,
    key        TEXT NOT NULL,
    action     TEXT NOT NULL CHECK (action IN ('insert', 'update', 'delete', 'publish', 'import')),
    old_json   TEXT,
    new_json   TEXT,
    reason     TEXT NOT NULL
);

-- Human decisions about individual buildings (replaces overrides.csv)
CREATE TABLE override (
    building_id                            TEXT PRIMARY KEY,
    primary_category_override              TEXT NOT NULL DEFAULT '',
    building_type_override                 TEXT NOT NULL DEFAULT '',
    operational_importance_override        TEXT NOT NULL DEFAULT '',
    operational_importance_driver_override TEXT NOT NULL DEFAULT '',
    manual_override_notes                  TEXT NOT NULL DEFAULT '',
    building_role_override                 TEXT NOT NULL DEFAULT '',
    review_decision                        TEXT NOT NULL DEFAULT '',
    verification_override                  TEXT NOT NULL DEFAULT '',
    exclude                                TEXT NOT NULL DEFAULT '',   -- 'Y' removes this building
    exclude_site                           TEXT NOT NULL DEFAULT '',   -- 'Y' removes the whole site
    decided_by                             TEXT NOT NULL DEFAULT '',
    decided_at                             TEXT NOT NULL DEFAULT ''
);

-- Append-only identity registries: IDs are never reused or deleted, only retired
CREATE TABLE building_registry (
    building_id    TEXT PRIMARY KEY,
    os_key         TEXT,
    osm_key        TEXT,
    created        TEXT NOT NULL,
    wkt            TEXT,
    first_seen_run TEXT NOT NULL,
    last_seen_run  TEXT NOT NULL,
    retired_run    TEXT
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
