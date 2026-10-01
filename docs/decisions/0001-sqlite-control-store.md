# 0001: Rules and decisions live in a SQLite store with a UI, plus text dumps for git

**Context.** The business rules started in an Excel workbook. Excel gave analysts a familiar screen but no change history,
no review, no safe way to publish a version, and it corrupts easily. CSV files in git fix history and review, but give
non-coders no validation or guard rails, and the requirement was a simple UI.

**Decision.** Store rules, role settings, register mapping, human decisions, identity registries and the run registry in one
SQLite file (`control/control.db`), edited through a Streamlit UI. Rules have a **draft** and immutable **published
versions**; every edit needs a user and a reason and is audited. The pipeline only reads published versions. Every save and
publish also writes deterministic CSV copies to `control/dumps/` so changes can be reviewed as ordinary git diffs.
All SQL lives in `control/store.py`.

**Consequences.**
- Good: history, versions, reproducibility ("which rules made this run?"), plain-English validation, a normalised model
  (a rule is stored once, with its tags).
- Cost: SQLite is a binary file in git (mitigated by the dumps; commit the db when releasing) and has one writer at a time.
  If several people must edit at once, repoint `store.py` at a shared database; nothing else touches SQL.
- The UI does not authenticate; the audit user is self-declared.
