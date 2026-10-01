"""Building role within a site: Standalone / Primary / Ancillary / Whole site / Unknown.

Rules, in order (thresholds and lists come from the 'Role settings' sheet of the rules workbook):
  1. Record is a site area with no buildings inside  -> Whole site (method 'OSM area (no buildings)')
  2. Only building at its site                       -> Standalone
  3. Point only (no footprint)                       -> Unknown
  4. OSM building=<ancillary value>                  -> Ancillary
  5. OSM building=<primary value>                    -> Primary
  6. Footprint >= minimum (per type, else default)   -> Primary, otherwise Ancillary
  7. A site always has at least one Primary: if none, its largest building is promoted.
"""


def assign_roles(recs, settings):
    recs = recs.copy()
    area = recs.geometry.area
    is_point = recs.geometry.geom_type.isin(["Point", "MultiPoint"])
    nb = recs.site_group.map(recs.site_group.value_counts())
    role, reason = [], []
    for i in recs.index:
        r = recs.loc[i]
        btag = r.osm_building if isinstance(r.osm_building, str) else ""
        minimum = settings["per_type"].get(r.building_type, settings["min_area"])
        if r.method == "OSM area (no buildings)":
            role.append("Whole site"); reason.append("Record is the site area itself (no buildings found inside)")
        elif nb[i] == 1:
            role.append("Standalone"); reason.append("Only building at this site")
        elif is_point[i]:
            role.append("Unknown"); reason.append("Point feature with no footprint")
        elif btag in settings["ancillary"]:
            role.append("Ancillary"); reason.append(f"OSM building={btag}")
        elif btag in settings["primary"]:
            role.append("Primary"); reason.append(f"OSM building={btag}")
        elif area[i] >= minimum:
            role.append("Primary"); reason.append(f"Footprint {area[i]:.0f} m2 is at least {minimum:.0f} m2")
        else:
            role.append("Ancillary"); reason.append(f"Footprint {area[i]:.0f} m2 is under {minimum:.0f} m2")
    recs["role"] = role
    recs["role_reason"] = reason

    # every multi-building site needs at least one primary building
    primary_like = recs.role.isin(["Primary", "Standalone", "Whole site"])
    has_primary = primary_like.groupby(recs.site_group).transform("any")
    for g, grp in recs[~has_primary & ~is_point].groupby("site_group"):
        big = area[grp.index].idxmax()
        recs.at[big, "role"] = "Primary"
        recs.at[big, "role_reason"] = "Largest building at the site (none met the size or OSM-type test)"
    return recs
