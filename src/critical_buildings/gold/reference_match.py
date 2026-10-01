"""Match authoritative reference records (DfE / CQC / NHS ODS) to sites, add the ones we are missing.

A reference record belongs to a site when it is the right kind of building, close to the site, and (unless it is
practically on top of the site) has a similar name. Many reference records may belong to one site (e.g. an infant
and a junior school on one campus). Reference records with no site become new single-building records.

Verification per site:  Verified | Unverified | Not covered
  Verified      at least one authoritative record matched
  Unverified    the building type is covered by a reference dataset but nothing matched (maybe closed, mis-tagged)
  Not covered   no reference dataset exists for this building type
"""
import re
from difflib import SequenceMatcher
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import Point

from critical_buildings import paths
from critical_buildings import config
from critical_buildings.matching.names import name_similarity, strict_similarity
from critical_buildings.silver.reference import _norm_pc


def _c():
    return config.get().reference_match



def _compatible(ref_type, site_type, categories):
    if ref_type == site_type or frozenset((ref_type, site_type)) in _c().compatible_pairs:
        return True
    return categories.get(ref_type) == categories.get(site_type) and categories.get(ref_type) in config.get().matching.loose_categories


def match_reference(ref, sites, site_names, categories):
    """Return list of (ref_index, site_group, distance, name_sim) for accepted matches."""
    sites = sites.reset_index(drop=True)
    sidx = sites.sindex
    matches = []
    for ri, r in ref.iterrows():
        cand = sidx.query(r.geometry.buffer(_c().max_distance_m), predicate="intersects")
        scored = []
        for ci in cand:
            s = sites.iloc[ci]
            if not _compatible(r.building_type, s.building_type, categories):
                continue
            d = s.geometry.distance(r.geometry)
            sname = site_names.get(s.site_group, s["name"])
            nm = name_similarity(r.ref_name, sname) if sname else _c().close_min_name   # unnamed site: neutral
            scored.append((s.site_group, d, nm, r.building_type == s.building_type,
                           strict_similarity(r.ref_name, sname) if sname else 0.0))
        near = [x for x in scored if x[1] <= _c().close_m]
        sole = [x for x in scored if x[3] and x[1] <= _c().sole_exact_m]
        ok = [x for x in scored
              if (x[1] <= _c().close_m and (x[2] >= _c().close_min_name or len(near) == 1))
              or (x[1] <= _c().mid_m and x[2] >= _c().mid_min_name)
              or (x[1] <= _c().far_name_limit_m and x[2] >= _c().far_min_name)
              or (x[3] and x[1] <= _c().far_exact_m and x[4] >= 0.9)          # same type, near-identical full name
              or (len(sole) == 1 and x is sole[0])]
        if ok:
            best = max(ok, key=lambda x: x[2] + (1 - min(x[1], _c().far_name_limit_m) / _c().far_name_limit_m) * 0.6)
            matches.append((ri, best[0], best[1], best[2]))
    return matches


def _join(vals, sep=" | "):
    out, seen = [], set()
    for v in vals:
        v = "" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v)
        if v and v.casefold() not in seen:       # 'WEST SUFFOLK NHS' and 'West Suffolk NHS' are the same
            seen.add(v.casefold())
            out.append(v)
    return sep.join(out)



def explain_unverified(site, ref_all, ref_valid, categories):
    """Say why a site has no authoritative match: closed per register, or the nearest candidate we did not accept."""
    name = site["name"] if isinstance(site["name"], str) else ""
    if name and site.building_type in _c().school_types:
        closed = ref_all[(ref_all.ref_source == "DfE GIAS") & (ref_all.building_type == "IGNORE")
                         & ref_all.status.astype(str).str.lower().str.startswith("closed")]
        for _, r in closed.iterrows():
            d = site.geometry.distance(r.geometry)
            if d <= _c().conflict_m and strict_similarity(r.ref_name, name) >= 0.9:
                return "Closed per register", f"DfE GIAS lists '{r.ref_name}' (URN {r.ref_id}) as closed"
    best = None
    for _, r in ref_valid.iterrows():
        d = site.geometry.distance(r.geometry)
        if d > 300:
            continue
        nm = name_similarity(r.ref_name, name) if name else 0
        if (nm >= 0.3 or _compatible(r.building_type, site.building_type, categories)) \
                and (best is None or nm - d / 1000 > best[0]):
            best = (nm - d / 1000, r, d, nm)
    if best:
        _, r, d, nm = best
        return "Unverified", (f"Nearest authoritative record not matched: {r.ref_name} "
                              f"({r.ref_source}, {r.building_type}, {d:.0f} m, name match {nm:.2f})")
    return "Unverified", "No authoritative record found within 300 m"


def building_footprints(boundary, area=paths.DEFAULT_AREA):
    """OS Open Map Local Building polygons for the area (cached, used to give authoritative points a footprint)."""
    cache = paths.silver_conformed_dir(area) / "os_buildings.gpkg"
    if cache.exists():
        return gpd.read_file(cache)
    frames = []
    for f in paths.os_layer_paths("Building", tiles=sorted(config.load_area(area)["os_grid_squares"])):
        g = gpd.read_file(f, columns=["ID"], bbox=tuple(boundary.bounds))
        frames.append(g)
    b = gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), crs=27700)
    b["geometry"] = b.geometry.force_2d()
    b = b[b.geometry.representative_point().within(boundary)]
    b.to_file(cache, driver="GPKG")
    return b


def attach_reference(recs, sites, ref, tags, boundary, ref_all=None, pc=None, area=paths.DEFAULT_AREA):
    """Returns (recs, sites, gaps) with authoritative attributes on recs and new records for unmatched references."""
    categories = {t.building_type: t.category for t in tags}
    site_names = dict(zip(sites.site_group, sites.name))
    m = match_reference(ref, sites, site_names, categories)
    mdf = pd.DataFrame(m, columns=["ri", "site_group", "dist", "name_sim"])

    # When the register and OS/OSM put a site far apart, the register's own postcode says which one is wrong:
    #   OS/OSM site at the postcode, register point away  -> register coordinates are off: keep the match
    #   register point at the postcode, OS/OSM site away  -> the OS/OSM location is wrong or old: do not match
    kept_notes, moved_notes, unresolved_notes = {}, {}, {}
    if pc:
        site_geom = dict(zip(sites.site_group, sites.geometry))
        keep = []
        for row in mdf.itertuples():
            if row.dist <= _c().location_review_m:
                keep.append(row.Index)
                continue
            r = ref.loc[row.ri]
            xy = pc.get(_norm_pc(r.postcode))
            if xy is None:
                unresolved_notes[row.site_group] = (
                    f"register postcode {r.postcode} is not in Code-Point Open (possibly a new building)")
                keep.append(row.Index)
                continue
            pt = Point(xy)
            d_site, d_ref = site_geom[row.site_group].distance(pt), r.geometry.distance(pt)
            if d_site <= _c().postcode_agree_m and d_ref > _c().postcode_far_m:
                kept_notes[row.site_group] = (f"Register point is {d_ref:.0f} m from its own postcode {r.postcode}; "
                                              f"OS/OSM location kept")
                keep.append(row.Index)
            elif d_ref <= _c().postcode_agree_m and d_site > _c().postcode_far_m:
                moved_notes[row.site_group] = (
                    f"{r.ref_source} places '{r.ref_name}' at its postcode {r.postcode}, {d_site:.0f} m from this "
                    f"location: this location may be wrong or an old site")
            else:
                unresolved_notes[row.site_group] = (f"register point is {d_ref:.0f} m and this site {d_site:.0f} m "
                                                    f"from the register postcode {r.postcode}")
                keep.append(row.Index)
        mdf = mdf.loc[keep].reset_index(drop=True)
    # a same-named record of a different type right on an unverified site: the authoritative source wins
    sites_r = sites.reset_index(drop=True)
    sidx2 = sites_r.sindex
    done_sites, done_refs = set(mdf.site_group), set(mdf.ri)
    conflicts = []
    for ri, r in ref.iterrows():
        if ri in done_refs:
            continue
        for ci in sidx2.query(r.geometry.buffer(_c().conflict_m), predicate="intersects"):
            st = sites_r.iloc[ci]
            if st.site_group in done_sites or _compatible(r.building_type, st.building_type, categories):
                continue
            d = st.geometry.distance(r.geometry)
            if isinstance(st["name"], str) and st["name"] and strict_similarity(r.ref_name, st["name"]) >= 0.9 \
                    and d <= _c().conflict_m:
                conflicts.append((ri, st.site_group, d, 1.0))
                done_sites.add(st.site_group)
                break
    if conflicts:
        mdf = pd.concat([mdf, pd.DataFrame(conflicts, columns=mdf.columns)], ignore_index=True)
    conflict_sites = {c[1]: c[0] for c in conflicts}
    matched_refs = set(mdf.ri)
    by_site = {g: ref.loc[grp.ri] for g, grp in mdf.groupby("site_group")}

    recs = recs.copy()
    recs["classification_note"] = ""
    sites = sites.copy()
    orig_sites = sites.reset_index(drop=True)
    orig_idx = orig_sites.sindex
    rule_for = {}
    for t in sorted(tags, key=lambda t: (t.priority, t.rule_ref)):
        rule_for.setdefault(t.building_type, t)

    for g, ri in conflict_sites.items():
        r = ref.loc[ri]
        old_type = sites.loc[sites.site_group == g, "building_type"].iloc[0]
        rule = rule_for.get(r.building_type)
        if rule is None:
            continue
        mask = recs.site_group == g
        recs.loc[mask, ["category", "building_type", "importance", "driver", "rule_ref", "rule_notes"]] = [
            rule.category, rule.building_type, rule.importance, rule.driver, rule.rule_ref, rule.notes]
        recs.loc[mask, "classification_note"] = (
            f"Building Type set from {r.ref_source} ({r.ref_name}); OS/OSM said {old_type}")
        recs.loc[mask, "review"] = ""
        sites.loc[sites.site_group == g, ["building_type", "category", "importance"]] = [
            rule.building_type, rule.category, rule.importance]

    # ---- the school type settled by DfE: where DfE says every matched school at a site is one type, use it ----
    for g, rr in by_site.items():
        dfe_types = set(rr[rr.ref_source == "DfE GIAS"].building_type)
        site_row = sites.loc[sites.site_group == g]
        if len(dfe_types) != 1 or site_row.empty:
            continue
        new_type, old_type = next(iter(dfe_types)), site_row.building_type.iloc[0]
        if new_type == old_type or new_type not in rule_for or categories.get(new_type) not in config.get().matching.loose_categories                 or categories.get(old_type) != categories.get(new_type):
            continue
        if rule_for[new_type].priority > rule_for.get(old_type, rule_for[new_type]).priority:
            continue          # DfE only says 'School' (e.g. independent schools): keep the more specific OS/OSM type
        rule = rule_for[new_type]
        mask = recs.site_group == g
        recs.loc[mask, ["building_type", "importance", "driver", "rule_ref", "rule_notes"]] = [
            rule.building_type, rule.importance, rule.driver, rule.rule_ref, rule.notes]
        recs.loc[mask, "classification_note"] = f"Building Type set from DfE GIAS; OS/OSM said {old_type}"
        recs.loc[mask, "review"] = recs.loc[mask, "review"].map(
            lambda t: "; ".join(x for x in str(t).split("; ") if x and not x.startswith("OS rule")))
        sites.loc[sites.site_group == g, "building_type"] = new_type
        sites.loc[sites.site_group == g, "importance"] = rule.importance

    # a site confirmed by an authoritative source settles record-level merge doubts
    verified = {g for g in by_site}
    clear = recs.site_group.isin(verified) & (recs.source == "Both") & (recs.review != "")
    recs.loc[clear, "review"] = ""
    recs.loc[clear & (recs.confidence == "Low"), "confidence"] = "Medium"

    # ---- gaps: authoritative records with no site -> new records ----
    gaps = ref[~ref.index.isin(matched_refs)]
    new_recs, new_sites = [], []
    next_group = int(sites.site_group.max()) + 1 if len(sites) else 1
    if len(gaps):
        blds = building_footprints(boundary, area)
        bs = blds.sindex
        for ri, r in gaps.iterrows():
            rule = rule_for.get(r.building_type)
            if rule is None:
                continue
            hit = bs.query(r.geometry, predicate="within")
            if not len(hit):                      # point just outside a building: snap to the nearest one
                near = bs.query(r.geometry.buffer(_c().snap_m), predicate="intersects")
                if len(near):
                    hit = [min(near, key=lambda k: blds.geometry.iloc[k].distance(r.geometry))]
            geom = blds.geometry.iloc[hit[0]] if len(hit) else r.geometry
            # a gap is expected (these sources are more complete than OSM). Only doubt it if it has no footprint
            # or sits next to an existing, similarly named site (possible duplicate).
            reasons = [] if len(hit) else [f"{r.ref_source} point has no building within {_c().snap_m} m - check the location"]
            for ci in orig_idx.query(r.geometry.buffer(50), predicate="intersects"):
                os_ = orig_sites.iloc[ci]
                if os_["name"] and _compatible(r.building_type, os_.building_type, categories)                         and name_similarity(r.ref_name, os_["name"]) >= 0.5:
                    reasons.append(f"Possible duplicate of existing site '{os_['name']}' ({os_.building_type})")
                    break
            new_recs.append(dict(
                source=r.ref_source, source_feature_id=f"{r.ref_source}:{r.ref_id}", name=r.ref_name,
                method="Authoritative only", confidence="High" if len(hit) else "Medium", os_ref=None, osm_ref=None,
                review="; ".join(reasons),
                geometry=geom, refs=str(rule.rule_ref), category=rule.category, building_type=rule.building_type,
                importance=rule.importance, driver=rule.driver, rule_ref=rule.rule_ref, rule_notes=rule.notes,
                osm_building="", site_group=next_group, site_method="Authoritative source", _ref=ri))
            new_sites.append(dict(site_group=next_group, name=r.ref_name, building_type=rule.building_type,
                                  category=rule.category, importance=rule.importance, n_buildings=1,
                                  method="Authoritative source",
                                  geometry=geom if geom.geom_type != "Point" else geom.buffer(15)))
            by_site[next_group] = ref.loc[[ri]]
            next_group += 1
    if new_recs:
        nr = gpd.GeoDataFrame(new_recs, geometry="geometry", crs=27700)
        recs = pd.concat([recs, nr.drop(columns="_ref")], ignore_index=True)
        recs = gpd.GeoDataFrame(recs, geometry="geometry", crs=27700)
        sites = gpd.GeoDataFrame(pd.concat([sites, gpd.GeoDataFrame(new_sites, geometry="geometry", crs=27700)],
                                           ignore_index=True), geometry="geometry", crs=27700)

    # sources that disagree about where a site is by more than LOCATION_REVIEW_M need a human look (once per site)
    for g, d in mdf.groupby("site_group").dist.min().round().items():
        if d > _c().location_review_m and g not in kept_notes:
            first = recs.index[recs.site_group == g][0]
            note = f"Authoritative source puts this site {d:.0f} m away from the OS/OSM location"
            if g in unresolved_notes:
                note += f" ({unresolved_notes[g]})"
            recs.at[first, "review"] = "; ".join(x for x in [str(recs.at[first, "review"] or ""), note] if x)

    # ---- authoritative attributes per site ----
    covered = set(ref.building_type) - _c().not_covered
    offset = mdf.groupby("site_group").dist.min().round().to_dict()
    info = {}
    for g in sites.site_group:
        rr = by_site.get(g)
        stype = sites.loc[sites.site_group == g, "building_type"].iloc[0]
        if rr is None or not len(rr):
            if stype not in covered:
                info[g] = dict(Verification="Not covered")
            elif g in moved_notes:
                info[g] = {"Verification": "Unverified", "Verification Note": moved_notes[g]}
            else:
                srow = sites.loc[sites.site_group == g].iloc[0]
                v, note = explain_unverified(srow, ref_all if ref_all is not None else ref, ref, categories)
                info[g] = {"Verification": v, "Verification Note": note}
            continue
        cap_units = rr.capacity_unit.replace("", pd.NA).dropna().unique()
        cap = rr.capacity.dropna().astype(float).sum() if len(cap_units) == 1 else None
        info[g] = {
            "Verification": "Verified" if g not in {x["site_group"] for x in new_sites} else "Authoritative only",
            "Verification Note": kept_notes.get(g) or (f"Matched {offset[g]:.0f} m from the authoritative point"
                                                       if offset.get(g, 0) > _c().close_m else ""),
            "Authoritative Distance (m)": offset.get(g),
            "Authoritative Source": _join(rr.ref_source), "Authoritative ID": _join(rr.ref_id),
            "Authoritative Name": _join(rr.ref_name), "Authoritative Status": _join(rr.status),
            "Capacity": int(cap) if cap else None, "Capacity Unit": cap_units[0] if len(cap_units) == 1 and cap else "",
            "Authoritative Detail": _join(rr.detail), "CQC Rating": _join(rr.rating),
            "Operator / Trust": _join(rr.provider), "Authoritative Postcode": _join(rr.postcode),
            "UPRN": _join(rr.uprn), "ODS Code": _join(rr.ods_code),
        }
    cols = ["Verification", "Verification Note", "Authoritative Distance (m)", "Authoritative Source", "Authoritative ID", "Authoritative Name", "Authoritative Status",
            "Capacity", "Capacity Unit", "Authoritative Detail", "CQC Rating", "Operator / Trust",
            "Authoritative Postcode", "UPRN", "ODS Code"]
    info_df = pd.DataFrame.from_dict(info, orient="index").reindex(columns=cols)
    for c in cols:
        recs["ref_" + c] = recs.site_group.map(info_df[c])
        sites["ref_" + c] = sites.site_group.map(info_df[c])
    gap_log = ref.loc[[g for g in gaps.index]] if len(gaps) else gaps
    return recs, sites, gap_log, mdf
