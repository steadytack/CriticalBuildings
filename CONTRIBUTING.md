# Contributing

## Set up

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[ui,dev]"
python -m pytest -m "not slow"      # fast tests (seconds)
python -m pytest                    # everything, including the end-to-end regression (needs data/; about 1.5 min)
```

## Ground rules

- **Behaviour changes need a reason.** `tests/test_e2e_regression.py` reproduces `tests/golden/baseline.json`. If you
  change the matching logic or thresholds on purpose, the baseline will differ: review the differences, then regenerate
  with `python tests/golden_utils.py <run folder> tests/golden/baseline.json` and say why in the commit message.
- **Settings, not magic numbers.** Thresholds belong in `config/pipeline.yml` with a comment. Business rules belong in the
  rules store, not in code.
- **Paths come from `paths.py`.** Do not build paths from `__file__`.
- **SQL only in `control/store.py`.** Everything else uses its methods.
- **A new output column needs a description** in `publish/dictionary.py`, or the run stops.
- **Do not edit `control/dumps/`** (generated). Do not commit `data/`, `published/`, `archive/` or `.venv/`.

## Changing rules

Use the UI (`python -m critical_buildings ui`), then commit `control/control.db` and the updated `control/dumps/`.
Reviewers read the dumps. See [docs/governance.md](docs/governance.md).

## Adding a source

1. Add it to `config/sources.yml` (licence and attribution are copied into manifests and `ATTRIBUTION.txt`).
2. Write a fetcher in `src/critical_buildings/bronze/`.
3. Read it in `silver/`.
4. Add tests (see `tests/test_bronze.py` for serving files from a local web server).

## Commits

Small commits with a message that says *why*. Keep the golden regression green.
