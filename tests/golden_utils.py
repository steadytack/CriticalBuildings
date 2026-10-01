"""Helpers for the golden regression baseline.

The baseline proves the pipeline still produces the same buildings and sites after refactoring.
Columns that legitimately change between runs (dates, rule/run identifiers) are excluded from the comparison.
"""
import hashlib
import json
from pathlib import Path

import pandas as pd

VOLATILE = ["Run Date", "Last Edit Date", "Rule Version", "Run ID"]
FILES = {
    "buildings": ("critical_buildings.csv", "Building ID"),
    "sites": ("critical_sites.csv", "Site ID"),
    "summary": ("critical_buildings_summary.csv", "Building ID"),
}


def canonical_hash(csv_path, key, drop=VOLATILE):
    """sha256 of a CSV after sorting by `key`, dropping volatile columns and fixing column order."""
    df = pd.read_csv(csv_path, dtype=str).fillna("")
    df = df.drop(columns=[c for c in drop if c in df.columns])
    df = df.sort_values(key).reset_index(drop=True)
    df = df[sorted(df.columns)]
    return hashlib.sha256(df.to_csv(index=False).encode("utf-8")).hexdigest(), df


def describe(outputs_dir):
    outputs_dir = Path(outputs_dir)
    out = {}
    for name, (fname, key) in FILES.items():
        digest, df = canonical_hash(outputs_dir / fname, key)
        out[name] = {"file": fname, "rows": len(df), "columns": sorted(df.columns), "sha256": digest}
    b = pd.read_csv(outputs_dir / FILES["buildings"][0], dtype=str).fillna("")
    out["counts"] = {
        "by_source": b["Source"].value_counts().sort_index().to_dict(),
        "by_verification": b["Verification"].value_counts().sort_index().to_dict(),
        "by_building_role": b["Building Role"].value_counts().sort_index().to_dict(),
        "by_quality_band": b["Data Quality Band"].value_counts().sort_index().to_dict(),
    }
    return out


def compare(outputs_dir, baseline_path):
    """Return a list of human-readable differences (empty list = identical)."""
    base = json.loads(Path(baseline_path).read_text(encoding="utf-8"))
    now = describe(outputs_dir)
    diffs = []
    for name in FILES:
        for field in ("rows", "columns", "sha256"):
            if base[name][field] != now[name][field]:
                what = "" if field == "sha256" else f" (baseline {base[name][field]} vs now {now[name][field]})"
                diffs.append(f"{name}: {field} differs{what if field != 'columns' else ''}")
    if base["counts"] != now["counts"]:
        diffs.append("summary counts differ")
    return diffs


if __name__ == "__main__":
    import sys
    outputs, target = sys.argv[1], sys.argv[2]
    Path(target).write_text(json.dumps(describe(outputs), indent=2, sort_keys=True), encoding="utf-8")
    print("baseline written to", target)
