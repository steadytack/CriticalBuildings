"""The shape of the code: which modules may depend on which.

The repo holds more than one data product (critical buildings, the UPRN property table, more to come) that share one core. For a product
to be moved into its own repository one day, it must depend on the shared core and nothing else from another product. This test keeps
that true, so the split stays a small job and does not need untangling first.
"""
import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "critical_buildings"

# The shared core: fetching and the source catalogue, locations, locks, lineage, progress, settings, errors, the area outline and postcodes.
SHARED = {"areas", "config", "errors", "lineage", "locks", "paths", "postcodes", "progress", "source_checks", "sources", "__version__"}
# Remaining couplings, each to be removed when the code is split (sources builds the OpenStreetMap query from the critical-buildings rule tags).
KNOWN = {"sources": {"control"}}


def package_imports(path):
    """The first part of every critical_buildings module (or name) a file imports, e.g. {'paths', 'silver', 'properties'}."""
    found = set()
    for node in ast.walk(ast.parse(Path(path).read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "critical_buildings":
                found |= {a.name for a in node.names}
            elif node.module.startswith("critical_buildings."):
                found.add(node.module.split(".")[1])
        elif isinstance(node, ast.Import):
            found |= {a.name.split(".")[1] for a in node.names if a.name.startswith("critical_buildings.")}
    return found


def violations(files, allowed):
    return {f.relative_to(SRC).as_posix(): sorted(bad) for f in files
            if (bad := package_imports(f) - allowed - KNOWN.get(f.stem, set()))}


def test_the_property_table_depends_only_on_the_shared_core():
    files = sorted((SRC / "properties").glob("*.py"))
    assert files and not violations(files, SHARED | {"properties"}), violations(files, SHARED | {"properties"})


def test_the_shared_core_depends_on_no_product():
    files = [SRC / f"{m}.py" for m in sorted(SHARED) if (SRC / f"{m}.py").exists()]
    assert len(files) >= 9 and not violations(files, SHARED), violations(files, SHARED)


def test_the_checker_itself_notices_a_forbidden_import(tmp_path):
    bad = tmp_path / "x.py"
    bad.write_text("from critical_buildings import paths, silver\nfrom critical_buildings.runs import start\nimport critical_buildings.publish\n", encoding="utf-8")
    assert package_imports(bad) == {"paths", "silver", "runs", "publish"}
