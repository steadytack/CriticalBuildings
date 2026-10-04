"""Group building records into sites (a school, a hospital, an airfield ...).

Tier 1  OS Functional Site polygon        (named, authoritative)
Tier 2  OSM site / area polygon           (e.g. a school campus, military land, aerodrome)
Tier 3  Proximity cluster                 (same type, buildings within sites.cluster_gap_m of each other)
        Single building                   (nothing else nearby)
OS and OSM site polygons of the same category that overlap by >= sites.merge_overlap are one site.
"""
import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
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
    cat, geom = cs["category"].to_numpy(), cs.geometry.to_numpy()
    for i, k in zip(j.index.to_numpy(), j["index_right"].to_numpy().astype(int)):
        if k <= i or cat[i] != cat[k]:
            continue
        a, b = geom[i], geom[k]
        if a.intersection(b).area / max(min(a.area, b.area), 1e-9) >= _c().merge_overlap:
            parent[find(i)] = find(k)
    members = {}                                    # root -> candidate positions, in order
    for i in range(len(cs)):
        members.setdefault(find(i), []).append(i)
    origin, names, cats = cs["origin"].to_numpy(), cs["name"].to_numpy(), cs["category"].to_numpy()
    geoms, areas = cs.geometry.to_numpy(), cs.geometry.area.to_numpy()
    out = []
    for root in sorted(members):
        idx = members[root]
        os_idx = [i for i in idx if origin[i] == "OS Functional Site"]
        base = os_idx[0] if os_idx else idx[int(np.nanargmax(areas[idx]))]       # else the largest polygon
        name = next((n for n in [names[base]] + [names[i] for i in os_idx] + [names[i] for i in idx] if n), "")
        out.append({"name": name, "category": cats[base], "origin": " + ".join(sorted(set(origin[idx]))),
                    "geometry": geoms[base]})
    return gpd.GeoDataFrame(out, geometry="geometry", crs=27700)


def _most_common(names):
    """The most frequent name; ties go to the alphabetically first (what Series.mode()[0] gave); '' if there are none."""
    counts = {}
    for n in names:
        if isinstance(n, str):
            counts[n] = counts.get(n, 0) + 1
    return min(counts, key=lambda n: (-counts[n], n)) if counts else ""


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
        m_name, m_origin, m_geom = merged["name"].to_numpy(), merged["origin"].to_numpy(), merged.geometry.to_numpy()
        for m_idx in sorted(hit["index_right"].unique()):                  # one new site per polygon that holds buildings
            gid += 1
            site_gid[m_idx] = gid
            sites[gid] = {"name": m_name[m_idx], "method": m_origin[m_idx], "geometry": m_geom[m_idx]}
        recs.loc[hit.index, "site_group"] = [site_gid[m] for m in hit["index_right"]]       # one assignment, not one per site
        recs.loc[hit.index, "site_method"] = [m_origin[m] for m in hit["index_right"]]

    # buildings just outside a site outline that carry the site's own name belong to that site
    left = recs[recs.site_group.isna() & (recs["name"].fillna("") != "")]
    if len(left) and sites:
        keep = merged.reset_index(drop=True)
        kidx = keep.sindex
        k_cat, k_name = keep["category"].to_numpy(), keep["name"].to_numpy()
        for i, r in zip(left.index, left.itertuples(index=False)):
            for ci in kidx.query(r.geometry.buffer(_c().attach_m), predicate="intersects"):
                if (k_cat[ci] == r.category and isinstance(k_name[ci], str) and k_name[ci]
                        and k_name[ci].casefold() == str(r.name).casefold()):
                    g = site_gid.get(ci)
                    if g is not None:
                        recs.at[i, "site_group"] = g
                        recs.at[i, "site_method"] = sites[g]["method"]
                        break

    # tier 3: cluster what is left
    left = recs[recs.site_group.isna()]
    if len(left):
        key = left.category.where(left.category.isin(config.get().matching.loose_categories), left.building_type)
        half_gap = _c().cluster_gap_m / 2
        all_geom, all_name = recs.geometry.to_numpy(), recs["name"].to_numpy()
        pos_of = pd.Series(range(len(recs)), index=recs.index)          # recs label -> position
        set_idx, set_gid, set_method = [], [], []
        for k, grp in left.groupby(key):
            # quad_segs=16 is what Geometry.buffer() uses; shapely.buffer() defaults to 8 and would change the outlines
            u = unary_union(shapely.buffer(grp.geometry.to_numpy(), half_gap, quad_segs=16))
            parts = list(u.geoms) if u.geom_type == "MultiPolygon" else [u]
            pg = gpd.GeoDataFrame({"p": range(len(parts))}, geometry=parts, crs=27700)
            pts = gpd.GeoDataFrame(geometry=grp.geometry.representative_point().values, crs=27700, index=grp.index)
            j = gpd.sjoin(pts, pg, predicate="within")
            j = j[~j.index.duplicated(keep="first")]
            for p, members in j.groupby("p"):
                gid += 1
                pos = pos_of[members.index].to_numpy()
                method = "Single building" if len(pos) == 1 else "Proximity cluster"
                sites[gid] = {"name": _most_common([n for n in all_name[pos] if n]), "method": method,
                              "geometry": unary_union(list(all_geom[pos])).convex_hull.buffer(half_gap)}
                set_idx += list(members.index)
                set_gid += [gid] * len(pos)
                set_method += [method] * len(pos)
        if set_idx:                                                       # one assignment, not one per cluster
            recs.loc[set_idx, "site_group"] = set_gid
            recs.loc[set_idx, "site_method"] = set_method

    # single-member sites from tier 1/2 are really just one building
    cnt = recs.site_group.value_counts()
    single = recs.site_group.map(cnt) == 1
    recs.loc[single & ~recs.site_method.isin(["Single building"]), "site_method"] = "Single building"
    # one row per site, summarised for all sites at once (a groupby over each site separately is slow with many sites)
    def mode_per_site(col):
        """Most common value of `col` per site; ties go to the smallest value (like Series.mode()[0])."""
        n = recs.groupby(["site_group", col]).size().rename("n").reset_index()
        return n.sort_values(["site_group", "n", col], ascending=[True, False, True]).drop_duplicates("site_group") \
                .set_index("site_group")[col]

    types, categories = mode_per_site("building_type"), mode_per_site("category")
    size = recs.groupby("site_group").size()
    best_rank = recs.importance.map(IMP_RANK).groupby(recs.site_group).min()
    importance = best_rank.map({v: k for k, v in IMP_RANK.items()})
    first_name = {}
    for g, n in zip(recs.site_group, recs["name"]):
        if n and g not in first_name:
            first_name[g] = n
    rows = []
    for g in size.index:
        rows.append({"site_group": g, "name": sites[g]["name"] or first_name.get(g, ""),
                     "building_type": types[g], "category": categories[g],
                     "importance": importance[g], "n_buildings": int(size[g]),
                     "method": sites[g]["method"] if size[g] > 1 else "Single building",
                     "geometry": sites[g]["geometry"]})
    sdf = gpd.GeoDataFrame(rows, geometry="geometry", crs=27700)
    return recs, sdf
