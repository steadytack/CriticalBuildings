# Run lifecycle: multiple runs, previous data, archiving

Nothing is overwritten. Every layer keeps its history under an identifier, and a small set of rules decides what can be
tidied up.

## Identifiers

| Id | Looks like | Means |
|---|---|---|
| `run_id` | `20261001T145935_suffolk` | One execution of the pipeline. |
| `bronze key` | `9db1914296f9` | Hash of the exact bronze files a run read. |
| `silver key` | `1775330ea165` | Hash of bronze key + rule set + silver schema. |
| `release` | `v2026.10.0` | A named, immutable copy of one run. |
| rule `version_id` | `3` (label `v3-a1b2c3`) | A published rule set. |

## What happens to each layer when you run again

- **Bronze.** `fetch` downloads, compares checksums with the latest snapshot, and stores a new dated folder only if
  something changed. Old snapshots stay. A run reads `LATEST.json` (the pointer moves to the new snapshot after a fetch).
- **Silver.** Same bronze + same rules + same silver schema = same key = the cached build is reused (`reusing silver
  build ...`). Change a rule or fetch new data and the key changes, so a fresh build is made and the old one stays.
- **Gold.** Every run creates its own folder `data/gold/<area>/<run_id>/`. Nothing in it is edited afterwards.
  `CURRENT.json` points at the latest *successful* run; a failed run is marked failed and leaves no partial folder.
- **Previous run.** The new run is compared with the run `CURRENT.json` pointed at when it started. The differences
  (added, removed, reclassified, source changed) are stored in the run registry (`run_change`) and shown in
  `run_summary.xlsx`. If that folder has been archived the run is treated as a first run (no comparison).
- **Identity.** Building and Site IDs live in append-only registries in the control store, so they survive even if every
  gold folder is archived. IDs are never reused; absent buildings are *retired*, and a returning one gets its old ID.

## Run types

`python -m critical_buildings run --type dev|candidate` (default `dev`). `dev` = scratch work, first to be pruned.
`candidate` = a run you may release. `release` is set by the `release` command.

## Releases

`python -m critical_buildings release [NAME] [--run RUN_ID] [--notes ...]` copies the run's deliverables and `lineage.json`
to `published/<area>/<NAME>/`, verifies every checksum, writes `RELEASE.json`, and records it. Names are never reused. The
default name is `vYYYY.MM.n`. A release **protects** its run, the bronze snapshots it read and its silver build.

## Retention and archiving

`python -m critical_buildings archive` (dry run) lists what the policy would do; `--apply` does it.

| Layer | Kept | Older ones |
|---|---|---|
| bronze | latest 3 snapshots per source, plus `LATEST` | zipped into `archive/bronze/...` |
| silver | latest 2 builds per area (classified and conformed) | deleted (rebuilt from bronze and the rules) |
| gold | latest 5 runs, `CURRENT`, and any dev run younger than 14 days | candidate runs zipped into `archive/gold/...`; dev runs deleted |
| published | everything | never touched |

Never touched, whatever the policy: `LATEST`/`CURRENT` pointers and anything a release depends on. Archives are verified
(zip test, SHA-256 in a `.json` sidecar) before the original is removed. Restore with
`python -m critical_buildings restore gold <run_id>` or `restore bronze <source folder>/<snapshot>`.

Change the numbers in `config/pipeline.yml` under `retention`.

## Reproducing an old run

`lineage.json` in the run folder names the bronze snapshots (with checksums), the rule version, the settings hash and the
git commit. To reproduce: check out that commit, restore those snapshots (archive if needed), pin the rule version, and run.
