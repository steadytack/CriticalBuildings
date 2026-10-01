# 0004: Stable IDs come from append-only registries in the control store

**Context.** Downstream users link to `Building ID` and `Site ID`; IDs must survive refreshes. The first version kept IDs by
reading the previous run's output file. Testing the new run folders showed that with no previous output every site got a
new ID, which proved identity must not depend on an output file that can be missing or archived.

**Decision.** Two registries in the control store. `building_registry` maps OS/OSM keys (and footprints) to a Building ID and
remembers the Site ID last assigned; `site_registry` records Site IDs. Rows are never deleted: absent items are *retired*, a
returning item gets its old ID back, and numbers are never reused. A site keeps the ID most of its buildings had.

**Consequences.** IDs are stable across runs, archives and fresh clones that carry `control.db`. The registry grows slowly.
Anyone who runs the pipeline without the committed `control.db` would mint different IDs, so treat `control.db` as part of
the product.
