"""The only module that talks SQL. Everything else uses this API (so the backend could move to Postgres later).

Rules are edited as a *draft* (the live tables); Publish freezes them into an immutable numbered version that the
pipeline uses. Every edit needs a user and a reason and is written to change_log.
"""
import getpass
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from critical_buildings import paths
from critical_buildings.control import rules as rs

MIGRATIONS = [(1, "schema.sql"), (2, "migrations/002_runs.sql"), (3, "migrations/003_registry_site.sql"),
              (4, "migrations/004_run_stage.sql"),
              (5, "migrations/005_drop_operational.sql")]     # (schema version, file); applied in order
SCHEMA_VERSION = MIGRATIONS[-1][0]
STATE_SCHEMA = "state_schema.sql"
STATE_BACKUPS_KEEP = 10                                  # unlabelled state backups kept (labelled, per-release ones are kept for good)
STATE_LABELLED = re.compile(r"^state-\d{8}T\d{12}Z-.+\.db$")      # state-<timestamp>-<label>.db; the label may be a release name
# Tables of the operational state database (data/state.db), parents before children. Everything else is governed.
STATE_TABLES = ("building_registry", "site_registry", "run", "release", "run_change", "run_stage")

RULE_COLS = ["rule_ref", "primary_category", "building_type", "importance", "importance_driver", "priority", "active", "notes"]
TAG_COLS = ["id", "rule_ref", "source", "tag", "active", "notes"]
MAP_COLS = ["id", "sort_order", "source", "field", "match", "value", "building_type", "active", "notes"]
OVERRIDE_COLS = ["building_id", "primary_category_override", "building_type_override", "operational_importance_override",
                 "operational_importance_driver_override", "manual_override_notes", "building_role_override",
                 "review_decision", "verification_override", "exclude", "exclude_site", "decided_by", "decided_at"]
# override table column -> the column name the pipeline's apply_overrides expects
OVERRIDE_CSV_NAMES = {
    "building_id": "Building ID", "primary_category_override": "Primary Category Override",
    "building_type_override": "Building Type Override", "operational_importance_override": "Operational Importance Override",
    "operational_importance_driver_override": "Operational Importance Driver Override",
    "manual_override_notes": "Manual Override Notes", "building_role_override": "Building Role Override",
    "review_decision": "Review Decision", "verification_override": "Verification Override",
    "exclude": "Exclude (Y)", "exclude_site": "Exclude Site (Y)",
}
TABLE_KEYS = {"rule": "rule_ref", "rule_tag": "id", "reference_mapping": "id"}
TABLE_COLS = {"rule": RULE_COLS, "rule_tag": TAG_COLS, "reference_mapping": MAP_COLS}


def now():
    return datetime.now(UTC).isoformat(timespec="seconds")


def current_user():
    try:
        return getpass.getuser()
    except (OSError, KeyError, ImportError):      # no user name available (some services, containers)
        return "unknown"


class ControlError(Exception):
    pass


class ControlStore:
    def __init__(self, path=None, state_path=None):
        """path: the governed rules store (default governed/control.db, in git). state_path: the operational state
        database holding runs, releases and the ID registries (default data/state.db, not in git). When only `path` is
        given the state database sits next to it, so a store in a temp folder never touches the real state."""
        self.path = Path(path) if path else paths.CONTROL / "control.db"
        if state_path:
            self.state_path = Path(state_path)
        elif path:
            self.state_path = self.path.with_name(self.path.stem + ".state.db")
        else:
            self.state_path = paths.DATA / "state.db"
        for p in (self.path, self.state_path):
            p.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ------------------------------------------------------------ connections
    @staticmethod
    @contextmanager
    def _connect(path):
        c = sqlite3.connect(path)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        c.execute("PRAGMA journal_mode = DELETE")        # one file on disk (no -wal/-shm)
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    def conn(self):
        """The governed store: rules, versions, decisions and the audit trail (governed/control.db)."""
        return self._connect(self.path)

    def state_conn(self):
        """The operational state: runs, releases and the ID registries (data/state.db)."""
        return self._connect(self.state_path)

    def _init_schema(self):
        with self.state_conn() as c:
            if c.execute("PRAGMA user_version").fetchone()[0] < 1:
                c.executescript((Path(__file__).parent / STATE_SCHEMA).read_text(encoding="utf-8"))
                c.execute("PRAGMA user_version = 1")
        moved = False
        with self.conn() as c:
            version = c.execute("PRAGMA user_version").fetchone()[0]
            for target, filename in MIGRATIONS:
                if version < target:
                    if target == 5:
                        self._copy_legacy_state(c)
                        moved = True
                    c.executescript((Path(__file__).parent / filename).read_text(encoding="utf-8"))
                    c.execute(f"PRAGMA user_version = {target}")
                    version = target
        if moved:
            vacuum = sqlite3.connect(self.path)          # give the dropped tables' pages back so the committed file shrinks
            try:
                vacuum.execute("VACUUM")
            finally:
                vacuum.close()

    def _copy_legacy_state(self, governed):
        """One-off upgrade: copy the runs, releases and ID registries out of an older governed store into the state
        database before migration 5 drops them there. Repeating it after an interruption is harmless: rows already
        copied are left as they are."""
        governed.commit()                                # release the governed file's write lock before attaching it
        with self.state_conn() as s:
            s.execute("ATTACH DATABASE ? AS legacy", (str(self.path),))
            for table in STATE_TABLES:
                cols = ", ".join(r["name"] for r in s.execute(f"PRAGMA main.table_info({table})"))
                s.execute(f"INSERT OR IGNORE INTO main.{table} ({cols}) SELECT {cols} FROM legacy.{table}")
            s.commit()
            s.execute("DETACH DATABASE legacy")

    def _log(self, c, user, table, key, action, old, new, reason):
        c.execute("INSERT INTO change_log (ts, user, table_name, key, action, old_json, new_json, reason) VALUES (?,?,?,?,?,?,?,?)",
                  (now(), user, table, str(key), action, json.dumps(old) if old is not None else None,
                   json.dumps(new) if new is not None else None, reason))

    # ------------------------------------------------------------ reads
    def log_setting_change(self, user, setting, old, new, reason):
        """Audit entry for an edit to settings.yml (made from the UI)."""
        if not reason or not reason.strip():
            raise ControlError("Please give a reason for the change.")
        with self.conn() as c:
            self._log(c, user, "settings.yml", setting, "update", old, new, reason.strip())

    def table_df(self, table, order=None):
        with (self.state_conn() if table in STATE_TABLES else self.conn()) as c:
            return pd.read_sql_query(f"SELECT * FROM {table}" + (f" ORDER BY {order}" if order else ""), c)

    def draft_snapshot(self):
        """The live (draft) tables as a snapshot dict."""
        with self.conn() as c:
            rules = [dict(r) for r in c.execute("SELECT * FROM rule ORDER BY rule_ref")]
            tags = [dict(r) for r in c.execute("SELECT * FROM rule_tag ORDER BY id")]
            mapping = [dict(r) for r in c.execute("SELECT * FROM reference_mapping ORDER BY sort_order, id")]
            role_set = {r["key"]: r["value"] for r in c.execute("SELECT * FROM role_setting")}
            osm_vals = [dict(r) for r in c.execute("SELECT kind, value FROM role_osm_value ORDER BY kind, value")]
            per_type = {r["building_type"]: r["min_area"] for r in c.execute("SELECT * FROM role_type_min ORDER BY building_type")}
        for r in rules:
            r["active"] = bool(r["active"])
        for t in tags:
            t["active"] = bool(t["active"])
        for m in mapping:
            m["active"] = bool(m["active"])
        return {
            "rules": rules, "tags": tags,
            "role": {"min_area": float(role_set.get("min_area", 150)),
                     "ancillary": [v["value"] for v in osm_vals if v["kind"] == "ancillary"],
                     "primary": [v["value"] for v in osm_vals if v["kind"] == "primary"],
                     "per_type": per_type},
            "reference_mapping": mapping,
        }

    def ruleset(self, version_id=None):
        """The pipeline's view of a published rule set (latest by default), with validation results attached."""
        vid, snap = self.get_version(version_id)
        errors, warnings, tags = rs.check_rules(snap)
        errors = errors + rs.check_role_settings(snap) + rs.check_reference_mapping(snap)
        role = snap["role"]
        return rs.RuleSet(
            version_id=vid, label=rs.version_label(vid, snap), snapshot=snap, tags=tags,
            role={"min_area": float(role["min_area"]), "ancillary": set(role["ancillary"]), "primary": set(role["primary"]),
                      "per_type": {k: float(v) for k, v in role["per_type"].items()}},
            mapping=[{"source": m["source"], "field": m["field"], "match": m["match"], "value": m["value"],
                          "building_type": m["building_type"]} for m in snap["reference_mapping"] if m["active"]],
            errors=errors, warnings=warnings)

    def versions_df(self):
        with self.conn() as c:
            return pd.read_sql_query("SELECT version_id, created_at, created_by, approved_by, reason, content_hash "
                                     "FROM rule_set_version ORDER BY version_id DESC", c)

    def get_version(self, version_id=None):
        """(version_id, snapshot) of a published version; the latest if version_id is None. Raises if none exists."""
        with self.conn() as c:
            row = (c.execute("SELECT version_id, snapshot_json FROM rule_set_version WHERE version_id = ?", (version_id,)).fetchone()
                   if version_id else
                   c.execute("SELECT version_id, snapshot_json FROM rule_set_version ORDER BY version_id DESC LIMIT 1").fetchone())
        if row is None and version_id:
            have = ", ".join(str(v) for v in sorted(self.versions_df().version_id)) or "none"
            raise ControlError(f"Rule set version {version_id} does not exist. Published versions: {have}.")
        if row is None:
            raise ControlError("No published rule set yet. Publish one from the UI, or run: python -m critical_buildings control init")
        return row["version_id"], json.loads(row["snapshot_json"])

    def has_unpublished_changes(self):
        try:
            _, snap = self.get_version()
        except ControlError:
            return True
        return rs.content_hash(self.draft_snapshot()) != rs.content_hash(snap)

    def change_log_df(self, limit=500):
        with self.conn() as c:
            return pd.read_sql_query("SELECT ts, user, table_name, key, action, reason, old_json, new_json "
                                     "FROM change_log ORDER BY id DESC LIMIT ?", c, params=(limit,))

    # ------------------------------------------------------------ rule edits
    def apply_table(self, table, rows, user, reason):
        """Make `table` match `rows` (list of dicts): insert new keys, update changed rows, delete missing ones.
        Used by the UI's grid editors. Rows without a key (new) are inserted; each change is audited."""
        if not reason or not str(reason).strip():
            raise ControlError("A reason is required for every change.")
        cols, key = TABLE_COLS[table], TABLE_KEYS[table]
        with self.conn() as c:
            old = {r[key]: dict(r) for r in c.execute(f"SELECT * FROM {table}")}
            seen = set()
            for row in rows:
                new = {k: row.get(k) for k in cols if k != key or row.get(k) is not None}
                for bool_col in ("active",):
                    if bool_col in new:
                        new[bool_col] = 1 if new[bool_col] in (1, True, "Y", "y", "TRUE", "true") else 0
                k = new.get(key)
                if k is not None and k in old:
                    seen.add(k)
                    changed = {c_: v for c_, v in new.items() if old[k].get(c_) != v}
                    if changed:
                        sets = ", ".join(f"{c_} = ?" for c_ in changed)
                        c.execute(f"UPDATE {table} SET {sets} WHERE {key} = ?", (*changed.values(), k))
                        self._log(c, user, table, k, "update", {c_: old[k][c_] for c_ in changed}, changed, reason)
                else:
                    names = ", ".join(new)
                    cur = c.execute(f"INSERT INTO {table} ({names}) VALUES ({', '.join('?' * len(new))})", tuple(new.values()))
                    k = new.get(key) if new.get(key) is not None else cur.lastrowid
                    seen.add(k)
                    self._log(c, user, table, k, "insert", None, new, reason)
            for k, o in old.items():
                if k not in seen:
                    c.execute(f"DELETE FROM {table} WHERE {key} = ?", (k,))
                    self._log(c, user, table, k, "delete", o, None, reason)

    def set_role_settings(self, min_area, ancillary, primary, per_type, user, reason):
        if not reason or not str(reason).strip():
            raise ControlError("A reason is required for every change.")
        with self.conn() as c:
            old = self.draft_snapshot()["role"]
            c.execute("INSERT INTO role_setting (key, value) VALUES ('min_area', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                      (str(float(min_area)),))
            c.execute("DELETE FROM role_osm_value")
            c.executemany("INSERT INTO role_osm_value (kind, value) VALUES (?, ?)",
                          [("ancillary", v) for v in sorted(set(ancillary))] + [("primary", v) for v in sorted(set(primary))])
            c.execute("DELETE FROM role_type_min")
            c.executemany("INSERT INTO role_type_min (building_type, min_area) VALUES (?, ?)",
                          [(bt, float(v)) for bt, v in sorted(per_type.items()) if v not in (None, "")])
            new = {"min_area": float(min_area), "ancillary": sorted(set(ancillary)), "primary": sorted(set(primary)),
                   "per_type": {k: float(v) for k, v in sorted(per_type.items()) if v not in (None, "")}}
            if new != old:
                self._log(c, user, "role_settings", "all", "update", old, new, reason)

    # ------------------------------------------------------------ publish
    def publish(self, user, reason, approved_by=""):
        """Validate the draft and freeze it as the next immutable version. Returns the new version id."""
        if not reason or not str(reason).strip():
            raise ControlError("A reason is required to publish.")
        snap = self.draft_snapshot()
        errors, _ = rs.check_all(snap)
        if errors:
            raise ControlError("Cannot publish - fix these first:\n  " + "\n  ".join(errors))
        with self.conn() as c:
            last = c.execute("SELECT content_hash FROM rule_set_version ORDER BY version_id DESC LIMIT 1").fetchone()
            h = rs.content_hash(snap)
            if last and last["content_hash"] == h:
                raise ControlError("Nothing to publish: the draft is identical to the latest published version.")
            cur = c.execute("INSERT INTO rule_set_version (created_at, created_by, approved_by, reason, content_hash, snapshot_json) "
                            "VALUES (?,?,?,?,?,?)", (now(), user, approved_by, reason, h, json.dumps(snap, sort_keys=True)))
            self._log(c, user, "rule_set_version", cur.lastrowid, "publish", None, {"content_hash": h}, reason)
            return cur.lastrowid

    # ------------------------------------------------------------ overrides
    def overrides_df(self):
        """Overrides with the column names the pipeline's apply_overrides expects (strings, '' when empty)."""
        df = self.table_df("override", "building_id")
        return df[list(OVERRIDE_CSV_NAMES)].rename(columns=OVERRIDE_CSV_NAMES)

    def save_overrides(self, rows, user, reason):
        """Insert/update override rows (dicts with the table's column names). Columns a row does not mention keep
        their stored value, so a partial save never clears other decisions on the same building."""
        if not reason or not str(reason).strip():
            raise ControlError("A reason is required for every change.")
        with self.conn() as c:
            for row in rows:
                new = {k: ("" if row.get(k) is None else str(row.get(k))) for k in OVERRIDE_COLS if k in row}
                new["decided_by"] = new.get("decided_by") or user
                new["decided_at"] = new.get("decided_at") or now()
                bid = new["building_id"]
                old = c.execute("SELECT * FROM override WHERE building_id = ?", (bid,)).fetchone()
                merged = {**dict(old), **new} if old else new
                names = ", ".join(merged)
                c.execute(f"INSERT OR REPLACE INTO override ({names}) VALUES ({', '.join('?' * len(merged))})", tuple(merged.values()))
                self._log(c, user, "override", bid, "update" if old else "insert", dict(old) if old else None, new, reason)

    def delete_override(self, building_id, user, reason):
        with self.conn() as c:
            old = c.execute("SELECT * FROM override WHERE building_id = ?", (building_id,)).fetchone()
            if old:
                c.execute("DELETE FROM override WHERE building_id = ?", (building_id,))
                self._log(c, user, "override", building_id, "delete", dict(old), None, reason)

    # ------------------------------------------------------------ identity registries (append-only)
    def building_registry_df(self):
        """All registered buildings (including retired ones, so a returning building keeps its ID)."""
        df = self.table_df("building_registry", "building_id")
        return df.rename(columns={"building_id": "building_id"})[["building_id", "os_key", "osm_key", "created", "wkt"]]

    def save_building_registry(self, df, run_id):
        """df columns: building_id, os_key, osm_key, created, wkt. New IDs are inserted, seen ones refreshed,
        IDs absent from this run are marked retired (never deleted)."""
        with self.state_conn() as c:
            self._save_building_registry(c, df, run_id)

    def _save_building_registry(self, c, df, run_id):
        for r in df.itertuples(index=False):
            os_key = r.os_key if isinstance(r.os_key, str) else None
            osm_key = r.osm_key if isinstance(r.osm_key, str) else None
            c.execute(
                "INSERT INTO building_registry (building_id, os_key, osm_key, created, wkt, first_seen_run, last_seen_run, retired_run) "
                "VALUES (?,?,?,?,?,?,?,NULL) ON CONFLICT(building_id) DO UPDATE SET os_key = excluded.os_key, "
                "osm_key = excluded.osm_key, wkt = excluded.wkt, last_seen_run = excluded.last_seen_run, retired_run = NULL",
                (r.building_id, os_key, osm_key, r.created, r.wkt, run_id, run_id))
        c.execute("UPDATE building_registry SET retired_run = ? WHERE retired_run IS NULL AND last_seen_run != ?",
                  (run_id, run_id))

    def building_site_map(self):
        """{building_id: site_id it was last assigned to} (empty strings and NULLs are left out)."""
        with self.state_conn() as c:
            return {r["building_id"]: r["site_id"] for r in c.execute(
                "SELECT building_id, site_id FROM building_registry WHERE site_id IS NOT NULL AND site_id != ''")}

    def set_building_sites(self, mapping):
        """mapping: {building_id: site_id}. Called after Site IDs are assigned."""
        with self.state_conn() as c:
            self._set_building_sites(c, mapping)

    def _set_building_sites(self, c, mapping):
        c.executemany("UPDATE building_registry SET site_id = ? WHERE building_id = ?",
                      [(site, bid) for bid, site in mapping.items()])

    def site_registry_df(self):
        df = self.table_df("site_registry", "site_id")
        return df[["site_id", "created"]]

    def save_site_registry(self, created, run_id):
        """created: {site_id: created_date}. Same append-only rules as buildings."""
        with self.state_conn() as c:
            self._save_site_registry(c, created, run_id)

    def _save_site_registry(self, c, created, run_id):
        for sid, when in created.items():
            c.execute("INSERT INTO site_registry (site_id, created, first_seen_run, last_seen_run, retired_run) VALUES (?,?,?,?,NULL) "
                      "ON CONFLICT(site_id) DO UPDATE SET last_seen_run = excluded.last_seen_run, retired_run = NULL",
                      (sid, when, run_id, run_id))
        c.execute("UPDATE site_registry SET retired_run = ? WHERE retired_run IS NULL AND last_seen_run != ?", (run_id, run_id))

    def commit_identities(self, pending, run_id):
        """Write the building and site registries a run computed ({'buildings': df, 'sites': {...}, 'building_sites': {...}},
        see steps.assign_ids) in one transaction. Called only once the run has succeeded, so a failed run never
        mints IDs or retires buildings."""
        with self.state_conn() as c:
            if "buildings" in pending:
                self._save_building_registry(c, pending["buildings"], run_id)
            if "sites" in pending:
                self._save_site_registry(c, pending["sites"], run_id)
            if "building_sites" in pending:
                self._set_building_sites(c, pending["building_sites"])

    # ------------------------------------------------------------ runs and releases
    def insert_run(self, row):
        with self.state_conn() as c:
            names = ", ".join(row)
            c.execute(f"INSERT INTO run ({names}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))

    def update_run(self, run_id, **fields):
        with self.state_conn() as c:
            sets = ", ".join(f"{k} = ?" for k in fields)
            c.execute(f"UPDATE run SET {sets} WHERE run_id = ?", (*fields.values(), run_id))

    def get_run(self, run_id):
        with self.state_conn() as c:
            row = c.execute("SELECT * FROM run WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def save_stage(self, run_id, stage, **fields):
        """Insert or update what one stage of a run did (status, timing, counts)."""
        with self.state_conn() as c:
            cols = ["run_id", "stage", *fields]
            sets = ", ".join(f"{k} = excluded.{k}" for k in fields)
            c.execute(f"INSERT INTO run_stage ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
                      f"ON CONFLICT(run_id, stage) DO UPDATE SET {sets}", (run_id, stage, *fields.values()))

    def stages_df(self, run_id=None):
        with self.state_conn() as c:
            q = "SELECT * FROM run_stage" + (" WHERE run_id = ?" if run_id else "") + " ORDER BY started_at"
            return pd.read_sql_query(q, c, params=(run_id,) if run_id else ())

    def runs_df(self, area=None):
        with self.state_conn() as c:
            q = "SELECT * FROM run" + (" WHERE area = ?" if area else "") + " ORDER BY started_at DESC"
            return pd.read_sql_query(q, c, params=(area,) if area else ())

    def add_run_changes(self, run_id, changes_df):
        if changes_df is None or changes_df.empty:
            return
        with self.state_conn() as c:
            c.executemany(
                "INSERT INTO run_change (run_id, building_id, change, before_value, after_value, source_feature_id) VALUES (?,?,?,?,?,?)",
                [(run_id, r["Building ID"], r["Change"], r["Before / Type"], r["After"], r["Source Feature ID"])
                 for r in changes_df.to_dict("records")])

    def run_changes_df(self, run_id):
        with self.state_conn() as c:
            return pd.read_sql_query("SELECT * FROM run_change WHERE run_id = ?", c, params=(run_id,))

    def insert_release(self, release, area, run_id, user, notes=""):
        with self.state_conn() as c:
            c.execute("INSERT INTO release (release, area, run_id, released_at, released_by, notes) VALUES (?,?,?,?,?,?)",
                      (release, area, run_id, now(), user, notes))
            c.execute("UPDATE run SET run_type = 'release' WHERE run_id = ?", (run_id,))

    def releases_df(self):
        with self.state_conn() as c:
            return pd.read_sql_query("SELECT * FROM release ORDER BY released_at DESC", c)

    # ------------------------------------------------------------ backup of the operational state
    def backup_state(self, label="", dest_dir=None, keep=STATE_BACKUPS_KEEP):
        """Copy the state database (runs, releases, ID registries; not in git) to a timestamped file and return its path.

        Uses SQLite's online backup, so it is consistent even while something else has the file open. Backups with no
        `label` (after each run, or by hand) are pruned to the newest `keep`; labelled ones (one per release) are kept
        for good, because a release is what users link their own data to. Default folder: `backups/` next to the state
        database (data/backups)."""
        dest_dir = Path(dest_dir) if dest_dir else self.state_path.parent / "backups"
        dest_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")        # microseconds: two backups never share a name
        dest = dest_dir / (f"state-{stamp}-{label}.db" if label else f"state-{stamp}.db")
        src = sqlite3.connect(self.state_path)
        try:
            out = sqlite3.connect(dest)
            try:
                src.backup(out)
            finally:
                out.close()
        finally:
            src.close()
        for old in sorted(p for p in dest_dir.glob("state-*.db") if not STATE_LABELLED.search(p.name))[:-keep]:
            old.unlink(missing_ok=True)
        return dest
