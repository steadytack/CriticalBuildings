"""The rule set as a plain, immutable snapshot (dict) and the Tag objects the pipeline classifies with.

A snapshot is what 'Publish' stores in rule_set_version.snapshot_json:
    {"rules": [...], "tags": [...], "role": {...}, "reference_mapping": [...]}
Everything the pipeline needs from the governed store comes through a snapshot, so a historic run can be reproduced
from the version id it recorded.
"""
import hashlib
import json
from dataclasses import dataclass

# Tag keys we expect to see in OSM. A key outside this list gets a "did you mean?" hint.
KNOWN_OSM_KEYS = {
    "amenity", "healthcare", "emergency", "social_facility", "social_facility:for", "building",
    "school", "railway", "public_transport", "station", "landuse", "aeroway", "government",
    "military_service", "leisure", "telecom", "plant:output:hot_water", "plant:output:electricity",
    "military", "office", "man_made", "power", "tourism", "shop", "healthcare:speciality",
    "telecom:data_center",
}
OS_GROUPS = {
    "Emergency services", "Education", "Transport", "Government Buildings", "Miscellaneous", "Medical Care",
    "Utilities", "Sport And Entertainment",
    # OS Open Map Local themes
    "Air Transport", "Attraction And Leisure", "Cultural Facility", "Emergency Service", "Religious Buildings",
    "Retail", "Road Transport", "Sports Or Exercise Facility", "Water Transport", "Rail Transport",
}
IMPORTANCE = ("High", "Medium", "Low")
SOURCES = ("OSM", "OS", "MANUAL")
MAPPING_SOURCES = ("DfE", "CQC", "NHS ODS")
# OSM tags that describe an area (not a building): buildings inside them inherit the area's rule
AREA_TAGS = {("landuse", "port"), ("landuse", "military"), ("aeroway", "aerodrome")}


@dataclass
class Tag:
    """One active way of recognising a building type (one tag of one rule)."""
    rule_ref: int
    priority: int
    category: str
    building_type: str
    importance: str
    driver: str
    source: str      # OSM / OS / MANUAL
    key: str         # OSM key, or OS theme
    value: str       # OSM value ('*' = any), or OS classification
    raw: str
    notes: str = ""


def content_hash(snapshot):
    """Stable sha256 of a snapshot (key order and whitespace independent)."""
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def version_label(version_id, snapshot):
    """Short string stamped on every output record: v<id>-<first 6 hash chars>."""
    return f"v{version_id}-{content_hash(snapshot)[:6]}"


def split_tag(source, tag):
    """('OSM', 'amenity=fire_station') -> ('amenity', 'fire_station'); ('OS', 'Education > School') -> ('Education', 'School')."""
    if source == "OSM":
        key, value = tag.split("=", 1)
        return key, value
    key, value = [s.strip() for s in tag.split(">")]
    return key, value


@dataclass
class RuleSet:
    """A published rule set, ready for the pipeline: tags to classify with, role settings, register mapping."""
    version_id: int
    label: str                 # v<id>-<hash>, stamped on every output record
    snapshot: dict
    tags: list
    role: dict                 # min_area, ancillary (set), primary (set), per_type
    mapping: list              # ACTIVE reference mapping rows, in order (first match wins)
    errors: list
    warnings: list

    @property
    def priorities(self):
        """{rule_ref: priority}: lower wins when a feature matches several rules."""
        return {t.rule_ref: t.priority for t in self.tags}

    def require_valid(self):
        if self.errors:
            raise SystemExit("Rules check FAILED:\n  " + "\n  ".join(self.errors))
        return self
