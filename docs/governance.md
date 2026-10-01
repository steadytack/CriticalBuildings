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
repository behind your organisation's access controls and review `control/dumps/` changes in git (below).

## Changing the rules

1. Edit in the UI (`python -m critical_buildings ui`). Every edit needs a **name** and a **reason** and is written to the
   audit trail (`change_log`: who, when, table, old value, new value, why).
2. Edits go to a **draft**. The pipeline never sees a draft.
3. **Publish** validates the draft (plain-English checks) and freezes it as a numbered, immutable version with a content
   hash. Optionally record who approved it.
4. Publishing rewrites `control/dumps/*.csv`. **Commit those and `control.db`** so reviewers see the change as an ordinary
   git diff and can challenge it in a pull request.
5. The next `run` uses the latest published version and stamps it on every record (`Rule Version`, e.g. `v3-a1b2c3`).

Any historic run can be explained by its rule version: `Rule Version` -> `rule_set_version.snapshot_json`.

## Human decisions (overrides and review)

Decisions about individual buildings (accept a flagged match, exclude a closed site, correct a type, verify manually) are
stored in the control store with who decided and when, and applied after the automatic logic on every run. They survive
reruns. They are also in `control/dumps/override.csv` for review.

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

- A **release** is an immutable, checksum-verified copy of one run in `published/<area>/<name>/`, named `vYYYY.MM.n`.
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

Defined in `config/pipeline.yml` (`retention`) and applied by `archive` (dry run by default): latest 3 bronze snapshots per
source, latest 2 silver builds, latest 5 gold runs; older ones are archived (bronze, gold) or deleted (silver, dev runs).
Releases are kept forever. See [run-lifecycle.md](run-lifecycle.md).

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
