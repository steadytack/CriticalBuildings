"""Group building records into sites (a school, a hospital, an airfield ...).

Tier 1  OS Functional Site polygon        (named, authoritative)
Tier 2  OSM site / area polygon           (e.g. a school campus, military land, aerodrome)
Tier 3  Proximity cluster                 (same type, buildings within sites.cluster_gap_m of each other)
        Single building                   (nothing else nearby)
OS and OSM site polygons of the same category that overlap by >= sites.merge_overlap are one site.
"""
import geopandas as gpd
import pandas as pd
from shapely.ops import unary_union

from critical_buildings import config

IMP_RANK = {"High": 0, "Medium": 1, "Low": 2}


def _c():
    return config.get().sites


def _merge_candidates(cs):
    """Merge overlapping same-category site polygons; return one row per real site."""
    cs = cs.reset_index(drop=True)
    parent = list(range(len(cs)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    j = gpd.sjoin(cs[["geometry", "category"]], cs[["geometry", "category"]], predicate="intersects")
    for i, row in j.iterrows():
        k = int(row["index_right"])
        if k <= i or cs.at[i, "category"] != cs.at[k, "category"]:
            continue
        a, b = cs.at[i, "geometry"], cs.at[k, "geometry"]
        if a.intersection(b).area / max(min(a.area, b.area), 1e-9) >= _c().merge_overlap:
            parent[find(i)] = find(k)
    cs["grp"] = [find(i) for i in range(len(cs))]
    out = []
    for g, grp in cs.groupby("grp"):
        os_rows = grp[grp.origin == "OS Functional Site"]
        base = os_rows.iloc[0] if len(os_rows) else grp.loc[grp.geometry.area.idxmax()]
        name = next((n for n in [base["name"]] + list(os_rows["name"]) + list(grp["name"]) if n), "")
        out.append({"name": name, "category": base.category, "origin": " + ".join(sorted(set(grp.origin))),
                        "geometry": base.geometry})
    return gpd.GeoDataFrame(out, geometry="geometry", crs=27700)


def assign_sites(recs, site_candidates):
    """Return (recs with 'site_group' and 'site_method', sites DataFrame keyed by site_group)."""
    recs = recs.copy()
    recs["site_group"] = None
    recs["site_method"] = None
    sites = {}
    site_gid = {}
    merged = None
    gid = 0
    rep = gpd.GeoDataFrame({"category": recs.category.values}, geometry=recs.geometry.representative_point().values,
                           crs=27700, index=recs.index)
    if len(site_candidates):
        merged = _merge_candidates(site_candidates)
        merged["area"] = merged.geometry.area
        hit = gpd.sjoin(rep, merged[["geometry", "category", "area"]], predicate="within")
        hit = hit[hit.category_left == hit.category_right].sort_values("area")
        hit = hit[~hit.index.duplicated(keep="first")]       # smallest containing site wins
        for m_idx, grp in hit.groupby("index_right"):
            gid += 1
            site_gid[m_idx] = gid
            row = merged.loc[m_idx]
            sites[gid] = {"name": row["name"], "method": row.origin, "geometry": row.geometry}
            recs.loc[grp.index, "site_group"] = gid
            recs.loc[grp.index, "site_method"] = row.origin

    # buildings just outside a site outline that carry the site's own name belong to that site
    left = recs[recs.site_group.isna() & (recs["name"].fillna("") != "")]
    if len(left) and sites:
        keep = merged.reset_index(drop=True)
        kidx = keep.sindex
        for i, r in left.iterrows():
            for ci in kidx.query(r.geometry.buffer(_c().attach_m), predicate="intersects"):
                row = keep.iloc[ci]
                if (row.category == r.category and isinstance(row["name"], str) and row["name"]
                        and row["name"].casefold() == str(r["name"]).casefold()):
                    g = site_gid.get(ci)
                    if g is not None:
                        recs.at[i, "site_group"] = g
                        recs.at[i, "site_method"] = sites[g]["method"]
                        break

    # tier 3: cluster what is left
    left = recs[recs.site_group.isna()]
    if len(left):
        key = left.category.where(left.category.isin(config.get().matching.loose_categories), left.building_type)
        for k, grp in left.groupby(key):
            buf = [g.buffer(_c().cluster_gap_m / 2) for g in grp.geometry]
            u = unary_union(buf)
            parts = list(u.geoms) if u.geom_type == "MultiPolygon" else [u]
            pg = gpd.GeoDataFrame({"p": range(len(parts))}, geometry=parts, crs=27700)
            pts = gpd.GeoDataFrame(geometry=grp.geometry.representative_point().values, crs=27700, index=grp.index)
            j = gpd.sjoin(pts, pg, predicate="within")
            j = j[~j.index.duplicated(keep="first")]
            for p, members in j.groupby("p"):
                gid += 1
                m = recs.loc[members.index]
                method = "Single building" if len(m) == 1 else "Proximity cluster"
                names = [n for n in m["name"] if n]
                sites[gid] = {"name": pd.Series(names).mode().iloc[0] if names else "", "method": method,
                                  "geometry": unary_union(list(m.geometry)).convex_hull.buffer(_c().cluster_gap_m / 2)}
                recs.loc[members.index, "site_group"] = gid
                recs.loc[members.index, "site_method"] = method

    # single-member sites from tier 1/2 are really just one building
    cnt = recs.site_group.value_counts()
    single = recs.site_group.map(cnt) == 1
    recs.loc[single & ~recs.site_method.isin(["Single building"]), "site_method"] = "Single building"
    rows = []
    for g, m in recs.groupby("site_group"):
        rows.append({"site_group": g, "name": sites[g]["name"] or next((n for n in m["name"] if n), ""),
                         "building_type": m.building_type.mode().iloc[0], "category": m.category.mode().iloc[0],
                         "importance": min(m.importance, key=IMP_RANK.get), "n_buildings": len(m),
                         "method": sites[g]["method"] if len(m) > 1 else "Single building",
                         "geometry": sites[g]["geometry"]})
    sdf = gpd.GeoDataFrame(rows, geometry="geometry", crs=27700)
    return recs, sdf
