# Governance

How the data product is owned, changed, checked and shared. The mechanisms below are built into the tooling; the roles
are conventions the team should assign.

## Roles

| Role | Responsibility | Typical actions |
|---|---|---|
| **Rule owner** | Decides what counts as a critical building and how important each type is. | Edits and publishes rule versions in the UI. |
| **Data steward** | Owns source quality and the register mapping; resolves review items. | Review & decisions tab; register mapping; unverified sites. |
| **Approver** (optional) | Second pair of eyes before a rule version or release goes out. | Named in *Approved by* when publishing. |
| **Data engineer** | Runs and maintains the pipeline, fetches, archives. | `fetch`, `run`, `release`, `archive`, code changes. |
| **Consumer** | Uses the published files. | Reads the summary CSV and data dictionary. |

The UI records whatever name is typed in the sidebar; it does **not** authenticate. If you need proof of identity, put the
repository behind your organisation's access controls and review `governed/dumps/` changes in git (below).

## Changing the rules

1. Edit in the UI (`python -m critical_buildings ui`). Every edit needs a **name** and a **reason** and is written to the
   audit trail (`change_log`: who, when, table, old value, new value, why).
2. Edits go to a **draft**. The pipeline never sees a draft.
3. **Publish** validates the draft (plain-English checks) and freezes it as a numbered, immutable version with a content
   hash. Optionally record who approved it.
4. Publishing rewrites `governed/dumps/*.csv`. **Commit those and `governed/control.db`** so reviewers see the change as an ordinary
   git diff and can challenge it in a pull request.
5. The next `run` uses the latest published version and stamps it on every record (`Rule Version`, e.g. `v3-a1b2c3`).

Any historic run can be explained by its rule version: `Rule Version` -> `rule_set_version.snapshot_json`.

## Human decisions (overrides and review)

Decisions about individual buildings (accept a flagged match, exclude a closed site, correct a type, verify manually) are
stored in the control store with who decided and when, and applied after the automatic logic on every run. They survive
reruns. They are also in `governed/dumps/override.csv` for review.

## Quality checks that run automatically

| When | Check |
|---|---|
| Before a run (`check`) | Rules valid (plain-English errors), register mapping valid, settings valid, bronze data present. |
| Saving / publishing rules | Same rule checks; publish is blocked while errors exist. |
| Merging | Same-type requirement for proximity matches; ambiguous matches go to the review queue. |
| Register matching | Name + distance + type; a register point far from its own postcode is arbitrated by the postcode. |
| Every run | Every output column must have a description in the data dictionary or the run stops. |
| Every run | `lineage.json` with input checksums; row counts and changes versus the previous run recorded. |
| Code changes | The automated test suite, including a regression test that must reproduce the golden baseline outputs. |

## Releases

- A **release** is an immutable, checksum-verified copy of one run in `data/published/<area>/<name>/`, named `vYYYY.MM.n`.
- Releases are recorded in the control store; the run, bronze snapshots and silver build behind a release are protected
  from archiving forever.
- Before sharing outside the organisation: check the **OSM ODbL share-alike** point (below) and include `ATTRIBUTION.txt`.

## Licensing

| Source | Licence | Note |
|---|---|---|
| OpenStreetMap | ODbL 1.0 | Share-alike: a derived database may have to be offered under the same terms. **Get a legal view before external sharing.** |
| OS Open Map Local, Code-Point Open | OGL v3 | Acknowledge OS (text in `ATTRIBUTION.txt`). Code-Point also carries Royal Mail and ONS acknowledgements. |
| DfE GIAS, CQC, NHS ODS | OGL v3 | Acknowledge the publisher. |

## Retention

Defined in `settings.yml` (`retention`) and applied by `archive` (dry run by default): latest 3 bronze snapshots per
source, latest 2 silver builds, latest 5 gold runs; older ones are archived (bronze, gold) or deleted (silver, dev runs).
Releases are kept forever. See [guide.md](guide.md#runs-releases-and-archiving).

## Sensitivity

The dataset includes prisons, MoD sites and data centres, all sourced from open data. There is **no sensitivity flag or
access tiering yet**; if the product is shared beyond the organisation, decide whether some types should be withheld or
published at reduced precision.

## Known limitations and roadmap

- One pilot area (Suffolk); other areas are config-only but untested.
- Importance is by building type, not per building. The quality score is a heuristic, not a probability.
- UPRN counts and the DESNZ/BUUK columns are reserved but empty (UPRN open data not yet added).
- `Closed per register` means the registered organisation closed; the building may still be in use.
- The rules store is SQLite: one editor at a time. The store layer (`control/store.py`) is the only code that talks SQL, so
  moving to a shared database is contained.
- Possible next steps: building history table (SCD2) from the registries and run changes; sensitivity tiers; GB-scale runs
  from a Geofabrik extract instead of the Overpass API.

## Design decisions

Short records of why things are the way they are (Context, Decision, Consequences). Add a numbered entry when you make a decision a newcomer would otherwise have to guess at.

### 0001. Rules and decisions live in a SQLite store with a UI, plus text dumps for git

**Context.** The business rules started in an Excel workbook. Excel gave analysts a familiar screen but no change history,
no review, no safe way to publish a version, and it corrupts easily. CSV files in git fix history and review, but give
non-coders no validation or guard rails, and the requirement was a simple UI.

**Decision.** Store rules, role settings, register mapping, human decisions, identity registries and the run registry in one
SQLite file (`governed/control.db`), edited through a Streamlit UI. Rules have a **draft** and immutable **published
versions**; every edit needs a user and a reason and is audited. The pipeline only reads published versions. Every save and
publish also writes deterministic CSV copies to `governed/dumps/` so changes can be reviewed as ordinary git diffs.
All SQL lives in `control/store.py`.

**Consequences.**
- Good: history, versions, reproducibility ("which rules made this run?"), plain-English validation, a normalised model
  (a rule is stored once, with its tags).
- Cost: SQLite is a binary file in git (mitigated by the dumps; commit the db when releasing) and has one writer at a time.
  If several people must edit at once, repoint `store.py` at a shared database; nothing else touches SQL.
- The UI does not authenticate; the audit user is self-declared.

### 0002. Bronze / silver / gold layers, immutable runs, releases and archiving

**Context.** Data was a flat folder of raw files, caches and diagnostics, and each run overwrote the previous output.
Nobody could say which inputs produced a given file or get back to an earlier result.

**Decision.** Layer the data (see [guide](guide.md#layers)). Bronze is append-only dated snapshots with checksums.
Silver is derived and cached under a key (inputs + rules + schema). Gold is one immutable folder per run, with a `CURRENT`
pointer. A release is a verified, immutable copy. Runs, releases and what changed per run are in a registry. Retention is
configurable and **dry-run by default**; it never touches anything a release depends on.

**Consequences.**
- Good: any run is explainable and reproducible from its `lineage.json`; reruns are cheap (silver cache); history is kept
  without unbounded disk use.
- Cost: more folders and concepts. Mitigated by `paths.py` (one place for locations), the guide, and the CLI.

### 0003. GeoPackage for deliverables, GeoParquet/Parquet for silver, zips read in place

**Context.** Consumers use GIS tools and spreadsheets; the pipeline needs fast, type-preserving intermediate tables; the OS
downloads are ~190 MB of zipped shapefiles.

**Decision.** Deliver GeoPackage (spatial) and CSV (tabular). Store silver tables as GeoParquet/Parquet, which preserve
types exactly (checked: reusing silver reproduces identical outputs). Keep OS and Code-Point zips as downloaded and read
the shapefiles straight from the zip (GDAL `zip://`), so bronze stays raw and there is no extracted copy to manage.

**Consequences.** Smaller data folder (1.4 GB to under 0.5 GB), bronze is exactly what was downloaded, and silver can be
deleted and rebuilt. A GeoPackage column cannot be called `Geom` (the CSV keeps the WKT text; the GeoPackage holds the
real geometry).

### 0004. Stable IDs come from append-only registries in the control store

**Context.** Downstream users link to `Building ID` and `Site ID`; IDs must survive refreshes. The first version kept IDs by
reading the previous run's output file. Testing the new run folders showed that with no previous output every site got a
new ID, which proved identity must not depend on an output file that can be missing or archived.

**Decision.** Two registries in the control store. `building_registry` maps OS/OSM keys (and footprints) to a Building ID and
remembers the Site ID last assigned; `site_registry` records Site IDs. Rows are never deleted: absent items are *retired*, a
returning item gets its old ID back, and numbers are never reused. A site keeps the ID most of its buildings had.

**Consequences.** IDs are stable across runs, archives and fresh clones that carry `control.db`. The registry grows slowly.
Anyone who runs the pipeline without the committed `control.db` would mint different IDs, so treat `control.db` as part of
the product.

### 0005. DfE / CQC / NHS records verify and complete the map data instead of replacing it

**Context.** OSM and OS give footprints but patchy coverage (OSM had about 70 care homes where CQC lists about 196). The
registers give authoritative status, capacity and ratings but only a point and a name.

**Decision.** Keep OS/OSM footprints as the geometry. Match each register record to a site by building type, distance and
name similarity. A match **verifies** the site and attaches capacity, rating and operator. A register record with no site
becomes a new building (snapped to an OS footprint when one is within 20 m). A same-named record of another type
reclassifies the site. Sites a covered register does not recognise are marked *Unverified*, *Closed per register*, or
carry a note naming the nearest candidate. When the sources disagree on location by more than 500 m, the register's own
postcode says which one is wrong.

**Consequences.** Better coverage and a visible trust signal per site, at the cost of heuristics (all thresholds are in
`settings.yml` and documented in the data dictionary). Registered-organisation closures are not building closures.
