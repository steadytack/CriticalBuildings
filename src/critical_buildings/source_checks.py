"""Checks that a downloaded source file still has the layout the readers in silver.py expect.

The publishers (DfE, CQC, NHS ODS, Ordnance Survey) change their files without notice. Without these checks a renamed column
fails deep inside pandas with a bare KeyError, and the NHS ODS files, which have no header row and are read by column
position, would not fail at all: a shifted column would quietly put the wrong text into names and postcodes. Each check
raises a PipelineError that names the source, what is wrong and what to do about it.
"""
import re

from critical_buildings.errors import PipelineError

GIAS_COLUMNS = ["URN", "EstablishmentName", "EstablishmentStatus (name)", "TypeOfEstablishment (name)",
                "PhaseOfEducation (name)", "NumberOfPupils", "Trusts (name)", "Easting", "Northing"]
CQC_COLUMNS = ["Location ID", "Location Name", "Location ODS Code", "Location Latitude", "Location Longitude",
               "Location Postal Code", "Location UPRN ID", "Location Primary Inspection Category",
               "Location Latest Overall Rating", "Care homes beds", "Provider Name"]
OS_LAYER_FIELDS = {                       # OS Open Map Local layer -> the attribute fields the readers use
    "FunctionalSite": ["ID", "DISTNAME", "SITETHEME", "CLASSIFICA"],
    "ImportantBuilding": ["ID", "DISTNAME", "BUILDGTHEM", "CLASSIFICA"],
    "RailwayStation": ["ID", "DISTNAME", "CLASSIFICA"],
    "Building": ["ID"],
}
# NHS ODS files have no header row. file -> {column position: (what it holds, pattern its values must mostly match or None)}
CODE = re.compile(r"^[A-Z0-9]{1,10}$")
POSTCODE = re.compile(r"^[A-Z]{1,2}\d[A-Z\d]? ?\d[A-Z]{2}$", re.IGNORECASE)
ODS_LAYOUT = {
    "etr.csv": {0: ("organisation code", CODE), 1: ("trust name", None)},
    "ets.csv": {0: ("site code", CODE), 1: ("site name", None), 9: ("postcode", POSTCODE), 11: ("close date", None),
                14: ("parent trust code", None)},
    "epraccur.csv": {0: ("practice code", CODE), 1: ("practice name", None), 9: ("postcode", POSTCODE),
                     12: ("status", None), 14: ("parent code", None)},
}
MOSTLY = 0.5          # share of non-empty values that must look right: tolerant of odd rows, fails on a shifted column

ADVICE = ("The publisher may have changed the file. Compare it with the previous snapshot in data/bronze, then either "
          "update the reader in silver.py or keep using the earlier snapshot.")


def require_columns(have, needed, what):
    """Raise unless every column in `needed` is among `have`."""
    missing = [c for c in needed if c not in set(have)]
    if missing:
        raise PipelineError(f"{what} no longer has the column(s) {', '.join(repr(c) for c in missing)} that the pipeline "
                            f"reads. {ADVICE}")


def check_mapping_fields(mapping, source, have, what):
    """The register mapping (edited in the UI) names the column each rule tests: they must exist in the file, or the rule
    would silently never match."""
    wanted = sorted({m["field"] for m in mapping if m["source"] == source})
    missing = [f for f in wanted if f not in set(have)]
    if missing:
        raise PipelineError(f"The register mapping for {source} tests the column(s) {', '.join(repr(f) for f in missing)}, "
                            f"which {what} does not have. Fix the mapping (Reference data tab) or see whether the publisher "
                            "renamed the column.")


def check_ods_layout(name, sample):
    """`sample`: the first rows of an ODS file read without a header (a DataFrame of strings). Checks the columns the
    reader takes by position are there and hold what they should."""
    layout = ODS_LAYOUT[name]
    need = max(layout) + 1
    if len(sample) == 0:
        raise PipelineError(f"NHS ODS file {name} is empty. {ADVICE}")
    if sample.shape[1] < need:
        raise PipelineError(f"NHS ODS file {name} has {sample.shape[1]} columns but the reader needs at least {need}. {ADVICE}")
    for pos, (what, pattern) in layout.items():
        if pattern is None:
            continue
        values = [str(v).strip() for v in sample[pos].dropna() if str(v).strip()]
        ok = sum(bool(pattern.match(v)) for v in values)
        if not values or ok / len(values) < MOSTLY:
            raise PipelineError(f"NHS ODS file {name}: column {pos} should hold the {what} but only {ok} of {len(values)} "
                                f"values look like one. The columns have probably moved. {ADVICE}")
