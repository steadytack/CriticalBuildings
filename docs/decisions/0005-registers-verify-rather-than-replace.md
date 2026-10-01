# 0005: DfE / CQC / NHS records verify and complete the map data instead of replacing it

**Context.** OSM and OS give footprints but patchy coverage (OSM had about 70 care homes where CQC lists about 196). The
registers give authoritative status, capacity and ratings but only a point and a name.

**Decision.** Keep OS/OSM footprints as the geometry. Match each register record to a site by building type, distance and
name similarity. A match **verifies** the site and attaches capacity, rating and operator. A register record with no site
becomes a new building (snapped to an OS footprint when one is within 20 m). A same-named record of another type
reclassifies the site. Sites a covered register does not recognise are marked *Unverified*, *Closed per register*, or
carry a note naming the nearest candidate. When the sources disagree on location by more than 500 m, the register's own
postcode says which one is wrong.

**Consequences.** Better coverage and a visible trust signal per site, at the cost of heuristics (all thresholds are in
`config/pipeline.yml` and documented in the data dictionary). Registered-organisation closures are not building closures.
