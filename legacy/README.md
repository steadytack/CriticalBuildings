# legacy/

Kept for provenance, not used by the pipeline.

| Item | What it is |
|---|---|
| `Critcal Building Data Product.xlsx` | The original rules workbook as first supplied (the typo in the name is original). |
| `rules_workbook_tidied_2026-10-01.xlsx` | The tidied workbook (one tag per row, Open Map Local names, role settings, register mapping) that was imported into `control/control.db`. |
| `migrations/` | One-off scripts that were run once during the restructure (rules tidy-up, data move into bronze, registry backfill). They are not importable. |
| `baseline_2026-10-01/` | Outputs before the restructure (not in git). The golden regression baseline derived from them is `tests/golden/baseline.json`. |
| `*.bak` | Safety copies of the control database taken before risky steps (not in git). |
