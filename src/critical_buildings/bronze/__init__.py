"""Bronze layer: raw downloads, exactly as received, in immutable dated snapshots with manifests."""
import logging

log = logging.getLogger(__name__)
ALL = ("osm", "os", "codepoint", "dfe", "cqc", "nhs")


def fetch_all(rules, area="suffolk", only=None, dry_run=False):
    """Fetch every source (or the named subset). Returns {source: result}. Never overwrites existing snapshots."""
    from critical_buildings.bronze import fetch_os, fetch_osm, fetch_registers
    jobs = {
        "osm": lambda: fetch_osm.fetch(rules, area, dry_run),
        "os": lambda: fetch_os.fetch_open_map_local(area, dry_run),
        "codepoint": lambda: fetch_os.fetch_codepoint(dry_run),
        "dfe": lambda: fetch_registers.fetch_dfe(dry_run),
        "cqc": lambda: fetch_registers.fetch_cqc(dry_run),
        "nhs": lambda: fetch_registers.fetch_nhs(dry_run),
    }
    out = {}
    for name in (only or ALL):
        log.info("== fetching %s", name)
        out[name] = jobs[name]()
    return out
