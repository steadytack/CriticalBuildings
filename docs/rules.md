# Rules: how they work and how to change them

## What a rule is

A **rule** says "buildings recognised by any of its tags are this **building type**". It has:

| Field | Meaning |
|---|---|
| Rule Ref | A number identifying the rule. |
| Primary Category / Building Type | Labels in the output (Emergency / Fire Station). |
| Operational Importance + Driver | High, Medium or Low, with the plain-English reason. |
| Priority | When a feature matches several rules, the **lowest number wins** (50 = normal, 90 = generic fallback). |
| Active | Switch a rule off without deleting it. |

Each rule has one or more **tags**, which say how to recognise it:

| Source | Format | Example |
|---|---|---|
| OSM | `key=value` (no spaces; `key=*` = any value) | `amenity=fire_station` |
| OS | `Theme > Classification` | `Emergency Service > Fire Station` |
| MANUAL | (blank) | Rule 34, "Commercially or Reputationally Important": only ever set by a human decision |

Tags can be switched off individually. Tags are checked in the order they were entered.

## Other governed settings

- **Role settings:** the footprint size (default 150 m²) above which a building at a multi-building site is *Primary*,
  the OSM `building=` values that are always Ancillary or Primary, and optional per-type minimums.
- **Register mapping:** how DfE, CQC and NHS ODS records become your building types (for example CQC
  "Care home? = Y" becomes Care Home; DfE phase "primary" becomes Primary School). Rows are checked top to bottom within
  a source and the first match wins; `IGNORE` drops a record.

## Changing rules

Use the editor: `python -m critical_buildings ui`.

1. **Rules tab**: edit the rule grid and the tag grid. Add a rule with *Add a new rule*. Untick *Active* to stop using one.
2. The **check of your edits** panel explains any problem in plain English and shows what your edit would change.
3. **Save to the draft** (needs your name and a reason).
4. **Publish** in the sidebar when ready. Runs use only published versions.

If you prefer to read rules in a spreadsheet: `python -m critical_buildings export-excel` (read-only).

## Where the rules live and why SQLite

Rules are in `control/control.db`, a single-file SQLite database, because they need edit history, versions and a UI,
which a spreadsheet or a pile of CSVs cannot give safely. The normalised model stores each rule once with its tags
(`rule`, `rule_tag`), the role settings (`role_setting`, `role_osm_value`, `role_type_min`), the register mapping
(`reference_mapping`), numbered immutable versions (`rule_set_version`) and the audit trail (`change_log`).

A binary file does not diff in git, so every save and publish also rewrites plain-text copies in `control/dumps/`.
Review those in pull requests. See [decisions/0001-sqlite-control-store.md](decisions/0001-sqlite-control-store.md).

## Original workbook

The rules started life in an Excel workbook, kept in `legacy/`. It was imported once. It is no longer used.
