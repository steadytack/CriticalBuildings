"""Match authoritative reference records (DfE / CQC / NHS ODS) to sites, add the ones we are missing.

A reference record belongs to a site when it is the right kind of building, close to the site, and (unless it is
practically on top of the site) has a similar name. Many reference records may belong to one site (e.g. an infant
and a junior school on one campus). Reference records with no site become new single-building records.

Verification per site:  Verified | Unverified | Not covered
  Verified      at least one authoritative record matched
  Unverified    the building type is covered by a reference dataset but nothing matched (maybe closed, mis-tagged)
  Not covered   no reference dataset exists for this building type
"""

import hashlib

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from pyogrio.raw import open_arrow
from shapely.geometry import Point

from critical_buildings import config, paths
from critical_buildings.gold.merge import (
    name_similarity,
    strict_similarity,
    strict_similarity_at_least,
)
from critical_buildings.silver import _norm_pc


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
    # plain arrays: indexing the frame row by row for every candidate is far slower than the matching itself
    s_geom, s_type, s_group, s_name = (sites.geometry.to_numpy(), sites["building_type"].to_numpy(),
                                       sites["site_group"].to_numpy(), sites["name"].to_numpy())
    c = _c()
    # What is looked up for every register record, worked out once: the name a site is compared under, and which site types a
    # register type may match (a boolean per distinct site type, not a Python call per candidate site).
    eff_name = np.array([site_names.get(g, n) for g, n in zip(s_group, s_name, strict=True)], dtype=object)
    has_name = np.array([bool(n) for n in eff_name])
    site_types, type_code = np.unique(s_type.astype(str), return_inverse=True)
    compat_by_type = {}
    matches = []
    for ri, geom, r_type, r_name in zip(ref.index, ref.geometry, ref.building_type, ref.ref_name, strict=True):
        cand = sidx.query(geom.buffer(c.max_distance_m), predicate="intersects")
        if r_type not in compat_by_type:
            compat_by_type[r_type] = np.array([_compatible(r_type, t, categories) for t in site_types])
        cand = cand[compat_by_type[r_type][type_code[cand]]]
        if not len(cand):
            continue
        dist = shapely.distance(s_geom[cand], geom)
        same = s_type[cand] == r_type
        n_near = int((dist <= c.close_m).sum())
        sole = np.flatnonzero(same & (dist <= c.sole_exact_m))
        sole_only = sole[0] if len(sole) == 1 else -1
        names = {}                                       # name similarity, worked out only for the candidates that need it

        def name_sim(j, cand=cand, r_name=r_name, names=names):
            if j not in names:
                k = cand[j]
                names[j] = name_similarity(r_name, eff_name[k]) if has_name[cand[j]] else c.close_min_name   # unnamed site: neutral
            return names[j]

        ok = []
        for j in range(len(cand)):
            d = dist[j]
            # the first three rules look at the name only within far_name_limit_m: beyond it only the last two can accept
            near_name = name_sim(j) if d <= c.far_name_limit_m else None
            if ((d <= c.close_m and (near_name >= c.close_min_name or n_near == 1))
                    or (d <= c.mid_m and near_name >= c.mid_min_name)
                    or (d <= c.far_name_limit_m and near_name >= c.far_min_name)
                    or (same[j] and d <= c.far_exact_m and has_name[cand[j]]            # same type, near-identical full name
                        and strict_similarity_at_least(r_name, eff_name[cand[j]], 0.9))
                    or j == sole_only):
                ok.append(j)
        if ok:
            best = max(ok, key=lambda j: name_sim(j) + (1 - min(dist[j], c.far_name_limit_m) / c.far_name_limit_m) * 0.6)
            matches.append((ri, s_group[cand[best]], dist[best], name_sim(best)))
    return matches


def _join(vals, sep=" | "):
    out, seen = [], set()
    for v in vals:
        v = "" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v)
        if v and v.casefold() not in seen:       # 'WEST SUFFOLK NHS' and 'West Suffolk NHS' are the same
            seen.add(v.casefold())
            out.append(v)
    return sep.join(out)



class _Candidates:
    """The register records `explain_unverified` searches, as arrays (built once, not per unverified site)."""

    def __init__(self, ref_all, ref_valid):
        self.closed = ref_all[(ref_all.ref_source == "DfE GIAS") & (ref_all.building_type == "IGNORE")
                              & ref_all.status.astype(str).str.lower().str.startswith("closed")]
        self.closed_geom = self.closed.geometry.to_numpy()
        self.valid = ref_valid
        self.valid_geom = ref_valid.geometry.to_numpy()
        # spatial indexes: asking which records are within reach must not cost a pass over every record in the country
        self.closed_tree = shapely.STRtree(self.closed_geom)
        self.valid_tree = shapely.STRtree(self.valid_geom)


def explain_unverified(site, ref_all, ref_valid, categories, candidates=None):
    """Say why a site has no authoritative match: closed per register, or the nearest candidate we did not accept.

    `candidates` (a _Candidates) lets a caller explaining many sites build the record arrays once."""
    cand = candidates or _Candidates(ref_all, ref_valid)
    return _explain(site["name"], site.building_type, site.geometry, categories, cand)


def _open_namesake(name, building_type, geometry, categories, cand):
    """True when an open register record of a compatible type and a similar name is close to the site. Then a closed record of
    the same name nearby means the school was reorganised (an infant and a junior school merged into a primary), not that the
    site closed: the open record simply matched the other building site of the campus, because each record matches one site."""
    c = _c()
    for k in cand.valid_tree.query(geometry, predicate="dwithin", distance=c.open_namesake_m):
        r = cand.valid.iloc[k]
        if _compatible(r.building_type, building_type, categories) and name_similarity(r.ref_name, name) >= c.open_namesake_min_name:
            return True
    return False


def _explain(site_name, building_type, geometry, categories, cand):
    """explain_unverified for a site given as plain values."""
    name = site_name if isinstance(site_name, str) else ""
    if name and building_type in _c().school_types and not _open_namesake(name, building_type, geometry, categories, cand):
        near = np.sort(cand.closed_tree.query(geometry, predicate="dwithin", distance=_c().conflict_m))
        for k in near:                   # only the closed schools within reach, in register order
            r = cand.closed.iloc[k]
            if strict_similarity(r.ref_name, name) >= 0.9:
                return "Closed per register", f"DfE GIAS lists '{r.ref_name}' (URN {r.ref_id}) as closed"
    best = None
    within = np.sort(cand.valid_tree.query(geometry, predicate="dwithin", distance=300))      # records within 300 m, in register order
    for k, d in zip(within, shapely.distance(geometry, cand.valid_geom[within]), strict=True):
        r = cand.valid.iloc[k]
        nm = name_similarity(r.ref_name, name) if name else 0
        if (nm >= 0.3 or _compatible(r.building_type, building_type, categories)) \
                and (best is None or nm - d / 1000 > best[0]):
            best = (nm - d / 1000, r, d, nm)
    if best:
        _, r, d, nm = best
        return "Unverified", (f"Nearest authoritative record not matched: {r.ref_name} "
                              f"({r.ref_source}, {r.building_type}, {d:.0f} m, name match {nm:.2f})")
    return "Unverified", "No authoritative record found within 300 m"


def _empty_footprints():
    return gpd.GeoDataFrame({"ID": []}, geometry=[], crs=27700)


def building_footprints(boundary, points, area=paths.DEFAULT_AREA, near=None):
    """The OS Open Map Local Building polygons within `near` metres of any of `points`, in file order.

    Only these are needed (to give register points a footprint), and a country has tens of millions of buildings, so each tile's
    Building layer is streamed in batches and only the polygons near a point are kept: memory stays small whatever the area.
    The result is cached under the points it was made for, so a rerun with the same register data does not read the tiles again."""
    near = _c().snap_m if near is None else near
    if not len(points):
        return _empty_footprints()
    coords = np.round(np.array([(p.x, p.y) for p in points]), 1)
    name = "os_buildings_near_" + hashlib.sha256(coords.tobytes() + str(near).encode()).hexdigest()[:12] + ".gpkg"
    inputs = [("os_open_map_local", None), ("osm_overpass", "area_boundary.json")]     # the footprints and the boundary they are clipped to
    cache = paths.conformed_lookup(area, name, inputs)
    if cache is not None:
        return gpd.read_file(cache)
    zones = shapely.STRtree(shapely.buffer(np.asarray(points, dtype=object), near, quad_segs=16))     # quad_segs=16: what Geometry.buffer() uses
    wanted = tuple(shapely.total_bounds(zones.geometries))
    kept = []
    for f in paths.os_layer_paths("Building", tiles=sorted(config.load_area(area)["os_grid_squares"]), area=area):
        with open_arrow(f, columns=["ID"], bbox=wanted, batch_size=100_000, use_pyarrow=True) as (meta, reader):
            geom_col = meta["geometry_name"] or "wkb_geometry"
            for batch in reader:
                frame = batch.to_pandas()
                geoms = shapely.force_2d(shapely.from_wkb(frame[geom_col].to_numpy()))
                hit = np.unique(zones.query(geoms, predicate="intersects")[0])
                if len(hit):
                    kept.append(gpd.GeoDataFrame({"ID": frame["ID"].to_numpy()[hit]}, geometry=geoms[hit], crs=27700))
    if not kept:
        return _empty_footprints()
    b = gpd.GeoDataFrame(pd.concat(kept, ignore_index=True), crs=27700)
    b = b[b.geometry.representative_point().within(boundary)].reset_index(drop=True)
    if b.empty:
        return _empty_footprints()
    b.to_file(paths.silver_conformed_dir(area) / name, driver="GPKG")
    paths.conformed_stamp(area, name, inputs)
    return b


def attach_reference(recs, sites, ref, tags, boundary, ref_all=None, pc=None, area=paths.DEFAULT_AREA):
    """Match the register records to the sites and return (recs, sites, gaps, matches).

    recs and sites carry the authoritative attributes (plus new records for register entries that matched no site);
    gaps are the register records that became new buildings; matches is the table of accepted (record, site) pairs."""
    categories = {t.building_type: t.category for t in tags}
    site_names = dict(zip(sites.site_group, sites.name, strict=False))
    m = match_reference(ref, sites, site_names, categories)
    mdf = pd.DataFrame(m, columns=["ri", "site_group", "dist", "name_sim"])

    # When the register and OS/OSM put a site far apart, the register's own postcode says which one is wrong:
    #   OS/OSM site at the postcode, register point away  -> register coordinates are off: keep the match
    #   register point at the postcode, OS/OSM site away  -> the OS/OSM location is wrong or old: do not match
    kept_notes, moved_notes, unresolved_notes = {}, {}, {}
    if pc:
        site_geom = dict(zip(sites.site_group, sites.geometry, strict=False))
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
    st_geom, st_type, st_group, st_name = (sites_r.geometry.to_numpy(), sites_r["building_type"].to_numpy(),
                                           sites_r["site_group"].to_numpy(), sites_r["name"].to_numpy())
    for ri, geom, r_type, r_name in zip(ref.index, ref.geometry, ref.building_type, ref.ref_name, strict=False):
        if ri in done_refs:
            continue
        for ci in sidx2.query(geom.buffer(_c().conflict_m), predicate="intersects"):
            if st_group[ci] in done_sites or _compatible(r_type, st_type[ci], categories):
                continue
            d = st_geom[ci].distance(geom)
            if isinstance(st_name[ci], str) and st_name[ci] and strict_similarity(r_name, st_name[ci]) >= 0.9 \
                    and d <= _c().conflict_m:
                conflicts.append((ri, st_group[ci], d, 1.0))
                done_sites.add(st_group[ci])
                break
    if conflicts:
        mdf = pd.concat([mdf, pd.DataFrame(conflicts, columns=mdf.columns)], ignore_index=True)
    conflict_sites = {c[1]: c[0] for c in conflicts}
    matched_refs = set(mdf.ri)
    # What was a DataFrame per site (80,000 .loc lookups) is a list of positions into the register arrays.
    ref_cols = {c: ref[c].to_numpy() for c in ("ref_source", "ref_id", "ref_name", "status", "detail", "rating", "provider",
                                              "postcode", "uprn", "ods_code", "capacity", "capacity_unit", "building_type")}
    ref_pos = {label: i for i, label in enumerate(ref.index)}
    by_site = {}                   # site_group -> positions in `ref` of its matched register records, in match order
    for g, label in zip(mdf.site_group, mdf.ri, strict=True):
        by_site.setdefault(g, []).append(ref_pos[label])

    recs = recs.copy()
    recs["classification_note"] = ""
    sites = sites.copy()
    orig_sites = sites.reset_index(drop=True)
    orig_idx = orig_sites.sindex
    rule_for = {}
    for t in sorted(tags, key=lambda t: (t.priority, t.rule_ref)):
        rule_for.setdefault(t.building_type, t)

    # positions of each site's buildings and of the site's own row, so that changing one site does not scan every building
    rec_rows = recs.groupby("site_group").indices
    rcol = {c: recs.columns.get_loc(c) for c in recs.columns}
    site_row = {g: i for i, g in enumerate(sites.site_group)}
    scol = {c: sites.columns.get_loc(c) for c in sites.columns}

    def set_site(g, rec_values=(), site_values=()):
        for c, v in rec_values:
            recs.iloc[rec_rows[g], rcol[c]] = v
        for c, v in site_values:
            sites.iloc[site_row[g], scol[c]] = v

    for g, ri in conflict_sites.items():
        r = ref.loc[ri]
        old_type = sites.iloc[site_row[g], scol["building_type"]]
        rule = rule_for.get(r.building_type)
        if rule is None:
            continue
        set_site(g, [("category", rule.category), ("building_type", rule.building_type), ("importance", rule.importance),
                     ("driver", rule.driver), ("rule_ref", rule.rule_ref), ("rule_notes", rule.notes),
                     ("classification_note", f"Building Type set from {r.ref_source} ({r.ref_name}); OS/OSM said {old_type}"),
                     ("review", "")],
                 [("building_type", rule.building_type), ("category", rule.category), ("importance", rule.importance)])

    # ---- the school type settled by DfE: where DfE says every matched school at a site is one type, use it ----
    site_type = dict(zip(sites.site_group, sites["building_type"], strict=True))
    loose = config.get().matching.loose_categories
    for g, rows in by_site.items():
        dfe_types = {ref_cols["building_type"][i] for i in rows if ref_cols["ref_source"][i] == "DfE GIAS"}
        if len(dfe_types) != 1 or g not in site_type:
            continue
        new_type, old_type = next(iter(dfe_types)), site_type[g]
        if (new_type == old_type or new_type not in rule_for or categories.get(new_type) not in loose
                or categories.get(old_type) != categories.get(new_type)):
            continue
        if rule_for[new_type].priority > rule_for.get(old_type, rule_for[new_type]).priority:
            continue          # DfE only says 'School' (e.g. independent schools): keep the more specific OS/OSM type
        rule = rule_for[new_type]
        review = recs.iloc[rec_rows[g], rcol["review"]].map(
            lambda t: "; ".join(x for x in str(t).split("; ") if x and not x.startswith("OS rule")))
        set_site(g, [("building_type", rule.building_type), ("importance", rule.importance), ("driver", rule.driver),
                     ("rule_ref", rule.rule_ref), ("rule_notes", rule.notes),
                     ("classification_note", f"Building Type set from DfE GIAS; OS/OSM said {old_type}")],
                 [("building_type", new_type), ("importance", rule.importance)])
        recs.iloc[rec_rows[g], rcol["review"]] = review.to_numpy()
        site_type[g] = new_type

    # a site confirmed by an authoritative source settles record-level merge doubts
    verified = set(by_site)
    clear = recs.site_group.isin(verified) & (recs.source == "Both") & (recs.review != "")
    recs.loc[clear, "review"] = ""
    recs.loc[clear & (recs.confidence == "Low"), "confidence"] = "Medium"

    # ---- gaps: authoritative records with no site -> new records ----
    gaps = ref[~ref.index.isin(matched_refs)]
    new_recs, new_sites = [], []
    next_group = int(sites.site_group.max()) + 1 if len(sites) else 1
    if len(gaps):
        blds = building_footprints(boundary, list(gaps.geometry), area)
        bs = blds.sindex
        blds_geom = blds.geometry.to_numpy()
        os_name, os_type = orig_sites["name"].to_numpy(), orig_sites["building_type"].to_numpy()
        for ri, r in zip(gaps.index, gaps.itertuples(index=False), strict=False):
            rule = rule_for.get(r.building_type)
            if rule is None:
                continue
            hit = bs.query(r.geometry, predicate="within")
            if not len(hit):                      # point just outside a building: snap to the nearest one
                near = bs.query(r.geometry.buffer(_c().snap_m), predicate="intersects")
                if len(near):
                    hit = [min(near, key=lambda k: blds_geom[k].distance(r.geometry))]
            geom = blds_geom[hit[0]] if len(hit) else r.geometry
            # a gap is expected (these sources are more complete than OSM). Only doubt it if it has no footprint
            # or sits next to an existing, similarly named site (possible duplicate).
            reasons = [] if len(hit) else [f"{r.ref_source} point has no building within {_c().snap_m} m - check the location"]
            for ci in orig_idx.query(r.geometry.buffer(50), predicate="intersects"):
                if os_name[ci] and _compatible(r.building_type, os_type[ci], categories) \
                        and name_similarity(r.ref_name, os_name[ci]) >= 0.5:
                    reasons.append(f"Possible duplicate of existing site '{os_name[ci]}' ({os_type[ci]})")
                    break
            new_recs.append({
                "source": r.ref_source, "source_feature_id": f"{r.ref_source}:{r.ref_id}", "name": r.ref_name,
                "method": "Authoritative only", "confidence": "High" if len(hit) else "Medium", "os_ref": None, "osm_ref": None,
                "review": "; ".join(reasons),
                "geometry": geom, "refs": str(rule.rule_ref), "category": rule.category, "building_type": rule.building_type,
                "importance": rule.importance, "driver": rule.driver, "rule_ref": rule.rule_ref, "rule_notes": rule.notes,
                "osm_building": "", "site_group": next_group, "site_method": "Authoritative source", "_ref": ri})
            new_sites.append({"site_group": next_group, "name": r.ref_name, "building_type": rule.building_type,
                                  "category": rule.category, "importance": rule.importance, "n_buildings": 1,
                                  "method": "Authoritative source",
                                  "geometry": geom if geom.geom_type != "Point" else geom.buffer(15)})
            by_site[next_group] = [ref_pos[ri]]
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
    site_pos = {}                                          # site_group -> position of its first row (a mask per site is quadratic)
    for pos, g in enumerate(sites.site_group):
        site_pos.setdefault(g, pos)
    new_groups = {x["site_group"] for x in new_sites}
    explain_on = _Candidates(ref_all if ref_all is not None else ref, ref)
    s_type, s_name, s_geom = sites["building_type"].to_numpy(), sites["name"].to_numpy(), sites.geometry.to_numpy()

    def values(column, rows):
        return [ref_cols[column][i] for i in rows]

    for g in sites.site_group:
        rows = by_site.get(g)
        stype = s_type[site_pos[g]]
        if not rows:
            if stype not in covered:
                info[g] = {"Verification": "Not covered"}
            elif g in moved_notes:
                info[g] = {"Verification": "Unverified", "Verification Note": moved_notes[g]}
            else:
                v, note = _explain(s_name[site_pos[g]], stype, s_geom[site_pos[g]], categories, explain_on)
                info[g] = {"Verification": v, "Verification Note": note}
            continue
        cap_units = list(dict.fromkeys(u for u in values("capacity_unit", rows) if isinstance(u, str) and u != ""))
        cap = sum(float(c) for c in values("capacity", rows) if not pd.isna(c)) if len(cap_units) == 1 else None
        info[g] = {
            "Verification": "Verified" if g not in new_groups else "Authoritative only",
            "Verification Note": kept_notes.get(g) or (f"Matched {offset[g]:.0f} m from the authoritative point"
                                                       if offset.get(g, 0) > _c().close_m else ""),
            "Authoritative Distance (m)": offset.get(g),
            "Authoritative Source": _join(values("ref_source", rows)), "Authoritative ID": _join(values("ref_id", rows)),
            "Authoritative Name": _join(values("ref_name", rows)), "Authoritative Status": _join(values("status", rows)),
            "Capacity": int(cap) if cap else None, "Capacity Unit": cap_units[0] if len(cap_units) == 1 and cap else "",
            "Authoritative Detail": _join(values("detail", rows)), "CQC Rating": _join(values("rating", rows)),
            "Operator / Trust": _join(values("provider", rows)), "Authoritative Postcode": _join(values("postcode", rows)),
            "UPRN": _join(values("uprn", rows)), "ODS Code": _join(values("ods_code", rows)),
        }
    cols = ["Verification", "Verification Note", "Authoritative Distance (m)", "Authoritative Source", "Authoritative ID", "Authoritative Name", "Authoritative Status",
            "Capacity", "Capacity Unit", "Authoritative Detail", "CQC Rating", "Operator / Trust",
            "Authoritative Postcode", "UPRN", "ODS Code"]
    info_df = pd.DataFrame.from_dict(info, orient="index").reindex(columns=cols)
    for c in cols:
        recs["ref_" + c] = recs.site_group.map(info_df[c])
        sites["ref_" + c] = sites.site_group.map(info_df[c])
    gap_log = ref.loc[list(gaps.index)] if len(gaps) else gaps
    return recs, sites, gap_log, mdf
