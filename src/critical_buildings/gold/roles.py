"""Building role within a site: Standalone / Primary / Ancillary / Whole site / Unknown.

Rules, in order (thresholds and lists come from the role settings in the rules store; edit them in the UI):
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
    # plain arrays: a row at a time through recs.loc[i] was the third slowest step of a national run
    areas, points = area.to_numpy(), is_point.to_numpy()
    nb = recs.site_group.map(recs.site_group.value_counts()).to_numpy()
    btags = [v if isinstance(v, str) else "" for v in recs.osm_building]
    minimums = [settings["per_type"].get(t, settings["min_area"]) for t in recs.building_type]
    methods = recs.method.to_numpy()
    role, reason = [], []
    for i in range(len(recs)):
        btag, minimum = btags[i], minimums[i]
        if methods[i] == "OSM area (no buildings)":
            role.append("Whole site")
            reason.append("Record is the site area itself (no buildings found inside)")
        elif nb[i] == 1:
            role.append("Standalone")
            reason.append("Only building at this site")
        elif points[i]:
            role.append("Unknown")
            reason.append("Point feature with no footprint")
        elif btag in settings["ancillary"]:
            role.append("Ancillary")
            reason.append(f"OSM building={btag}")
        elif btag in settings["primary"]:
            role.append("Primary")
            reason.append(f"OSM building={btag}")
        elif areas[i] >= minimum:
            role.append("Primary")
            reason.append(f"Footprint {areas[i]:.0f} m2 is at least {minimum:.0f} m2")
        else:
            role.append("Ancillary")
            reason.append(f"Footprint {areas[i]:.0f} m2 is under {minimum:.0f} m2")
    recs["role"] = role
    recs["role_reason"] = reason

    # every multi-building site needs at least one primary building
    primary_like = recs.role.isin(["Primary", "Standalone", "Whole site"])
    has_primary = primary_like.groupby(recs.site_group).transform("any")
    for _, grp in recs[~has_primary & ~is_point].groupby("site_group"):
        big = area[grp.index].idxmax()
        recs.at[big, "role"] = "Primary"
        recs.at[big, "role_reason"] = "Largest building at the site (none met the size or OSM-type test)"
    return recs
