# 0002: Bronze / silver / gold layers, immutable runs, releases and archiving

**Context.** Data was a flat folder of raw files, caches and diagnostics, and each run overwrote the previous output.
Nobody could say which inputs produced a given file or get back to an earlier result.

**Decision.** Layer the data (see [architecture](../architecture.md)). Bronze is append-only dated snapshots with checksums.
Silver is derived and cached under a key (inputs + rules + schema). Gold is one immutable folder per run, with a `CURRENT`
pointer. A release is a verified, immutable copy. Runs, releases and what changed per run are in a registry. Retention is
configurable and **dry-run by default**; it never touches anything a release depends on.

**Consequences.**
- Good: any run is explainable and reproducible from its `lineage.json`; reruns are cheap (silver cache); history is kept
  without unbounded disk use.
- Cost: more folders and concepts. Mitigated by `paths.py` (one place for locations), `run-lifecycle.md`, and the CLI.
