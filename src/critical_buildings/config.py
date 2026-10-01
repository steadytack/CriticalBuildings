"""Load config files (config/areas/<area>.yml; pipeline settings are added in the config phase)."""
import yaml

from critical_buildings import paths


def load_area(name=paths.DEFAULT_AREA):
    p = paths.CONFIG / "areas" / f"{name}.yml"
    if not p.exists():
        known = ", ".join(sorted(f.stem for f in (paths.CONFIG / "areas").glob("*.yml")))
        raise SystemExit(f"Unknown area '{name}'. Known areas: {known}")
    return yaml.safe_load(p.read_text(encoding="utf-8"))
