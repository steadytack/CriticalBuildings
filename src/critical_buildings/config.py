"""Configuration: area files (config/areas/<area>.yml) and pipeline settings (config/pipeline.yml).

    from critical_buildings import config
    config.get().merge.point_distance_m        # read a setting
    with config.use({"merge": {"point_distance_m": 10}}): ...   # temporary override (tests)
"""
import copy
import hashlib
import json
from contextlib import contextmanager
from functools import lru_cache
from types import SimpleNamespace

import yaml

from critical_buildings import paths

SETTINGS_FILE = paths.CONFIG / "pipeline.yml"


def load_area(name=paths.DEFAULT_AREA):
    p = paths.CONFIG / "areas" / f"{name}.yml"
    if not p.exists():
        known = ", ".join(sorted(f.stem for f in (paths.CONFIG / "areas").glob("*.yml")))
        raise SystemExit(f"Unknown area '{name}'. Known areas: {known}")
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def _ns(raw):
    """Sections become attribute objects (settings.merge.point_distance_m); values inside a section stay plain
    dicts/lists, so lookup tables such as confidence_points keep working with Series.map."""
    return SimpleNamespace(**{section: SimpleNamespace(**values) for section, values in raw.items()})


def _build(raw):
    s = _ns(copy.deepcopy(raw))
    # convert list settings the code needs as sets
    s.matching.loose_categories = set(s.matching.loose_categories)
    rm = s.reference_match
    rm.compatible_pairs = {frozenset(p) for p in rm.compatible_pairs}
    rm.not_covered = set(rm.not_covered)
    rm.school_types = set(rm.school_types)
    rm.stop_words = set(rm.stop_words)
    return s


def _raw(path=None):
    return yaml.safe_load((path or SETTINGS_FILE).read_text(encoding="utf-8"))


_override = None


@lru_cache(maxsize=1)
def _cached():
    return _build(_raw())


def get():
    """The current settings (file values, or a temporary override from use())."""
    return _override if _override is not None else _cached()


@contextmanager
def use(changes):
    """Temporarily override settings, e.g. use({'merge': {'point_distance_m': 10}}). Used by tests."""
    global _override
    raw = _raw()
    for section, values in changes.items():
        raw[section].update(values)
    previous, _override = _override, _build(raw)
    try:
        yield _override
    finally:
        _override = previous


def settings_hash():
    """Short content hash of the effective settings (recorded in lineage so a run can be reproduced)."""
    raw = _raw()
    return hashlib.sha256(json.dumps(raw, sort_keys=True, default=str).encode()).hexdigest()[:12]


def validate():
    """Plain-English problems with config/pipeline.yml (empty list = fine)."""
    problems = []
    try:
        raw = _raw()
        _build(raw)
    except Exception as e:                       # noqa: BLE001 - report any malformed file clearly
        return [f"config/pipeline.yml could not be read: {e}"]
    for key, section in (("merge", ["point_distance_m", "overlap_fraction", "name_agree"]),
                         ("sites", ["cluster_gap_m", "attach_m", "merge_overlap"]),
                         ("quality", ["band_high_min", "band_medium_min"])):
        for k in section:
            v = raw.get(key, {}).get(k)
            if not isinstance(v, (int, float)) or v < 0:
                problems.append(f"config/pipeline.yml: {key}.{k} must be a non-negative number, found {v!r}")
    q = raw.get("quality", {})
    if q.get("band_medium_min", 0) >= q.get("band_high_min", 100):
        problems.append("config/pipeline.yml: quality.band_medium_min must be lower than quality.band_high_min")
    return problems
