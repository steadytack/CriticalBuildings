# Decision records

Short notes on why things are the way they are. Each has Context, Decision and Consequences. Add a new numbered file when
you make a decision a newcomer would otherwise have to guess at.

| # | Decision |
|---|---|
| [0001](0001-sqlite-control-store.md) | Rules and decisions live in a SQLite store with a UI, plus text dumps for git |
| [0002](0002-medallion-and-immutable-runs.md) | Bronze / silver / gold layers, immutable runs, releases and archiving |
| [0003](0003-file-formats.md) | GeoPackage for deliverables, GeoParquet/Parquet for silver, zips read in place |
| [0004](0004-append-only-identity-registries.md) | Stable IDs come from append-only registries in the control store |
| [0005](0005-registers-verify-rather-than-replace.md) | DfE / CQC / NHS records verify and complete the map data instead of replacing it |
