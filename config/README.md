# config/

Settings that are **technical tuning** (not business rules; those are in the rules store and edited in the UI).

| File | Purpose |
|---|---|
| `pipeline.yml` | Every matching, grouping and scoring threshold and the retention policy, each with a comment. Changing these changes the output: record why. `python -m critical_buildings check` validates the file. |
| `areas/<area>.yml` | One file per area: the OSM boundary (by government code) and the OS grid squares. |
| `sources.yml` | The source catalogue: URL, licence, attribution, refresh cadence. Copied into bronze manifests and `ATTRIBUTION.txt`. |
