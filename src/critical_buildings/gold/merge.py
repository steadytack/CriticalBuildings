"""Merge classified OSM and OS features into one record per building.

Match rule (thresholds in settings.yml, merge section): an OS building polygon and an OSM feature are the
same building when they have the same Primary Category (and, outside Education, the same Building Type) and either
overlap (the OSM polygon covers >= overlap_fraction of the OS polygon or contains its centre, or the OSM point lies
inside it) or are within point_distance_m of each other (lower confidence; the types must then also agree).
An OSM point is matched to only its single nearest OS polygon; an OSM polygon (e.g. a school site) can match
several OS building polygons.
"""
import re
from difflib import SequenceMatcher

import geopandas as gpd
import pandas as pd

from critical_buildings import config

IMP_RANK = {"High": 0, "Medium": 1, "Low": 2}
MERGED_COLUMNS = ["source", "source_feature_id", "name", "os_ref", "osm_ref", "method", "confidence", "review", "osm_building",
                  "geometry", "refs", "category", "building_type", "importance", "driver", "rule_ref", "rule_notes"]


def _c():
    return config.get().merge


def _pair_method(os_geom, osm_geom, max_d=None):
    """Return (method, distance) or None."""
    max_d = _c().point_distance_m if max_d is None else max_d
    d = os_geom.distance(osm_geom)
    if d > max_d:
        return None
    if osm_geom.geom_type in ("Polygon", "MultiPolygon"):
        inter = os_geom.intersection(osm_geom).area
        if os_geom.representative_point().within(osm_geom) or inter / max(os_geom.area, 1e-9) >= _c().overlap_fraction:
            return "overlap", d
        return "proximity", d
    return ("overlap", d) if d == 0 else ("proximity", d)


def merge(osm, os_, prio):
    """prio: {rule_ref: priority} used to choose between the OS rule and the OSM rule when both match."""
    osm = osm.reset_index(drop=True)
    os_ = os_.reset_index(drop=True)
    cand = gpd.sjoin(os_[["geometry"]], osm[["geometry"]], predicate="dwithin", distance=max(_c().point_distance_by_type.values(), default=_c().point_distance_m))
    pairs = []
    for os_i, row in cand.iterrows():
        o = int(row["index_right"])
        if os_.at[os_i, "category"] != osm.at[o, "category"]:
            continue
        if (os_.at[os_i, "category"] not in config.get().matching.loose_categories
                and os_.at[os_i, "building_type"] != osm.at[o, "building_type"]):
            continue
        # a building 'next to' another is only the same building if the types agree; different types must overlap
        m = _pair_method(os_.at[os_i, "geometry"], osm.at[o, "geometry"],
                         _c().point_distance_by_type.get(os_.at[os_i, "building_type"], _c().point_distance_m))
        if m and m[0] == "proximity" and os_.at[os_i, "building_type"] != osm.at[o, "building_type"]:
            m = None
        if m:
            pairs.append(dict(os=os_i, osm=o, method=m[0], dist=m[1],
                              same_type=os_.at[os_i, "building_type"] == osm.at[o, "building_type"],
                              osm_area=osm.at[o, "geometry"].area,
                              osm_point=osm.at[o, "geometry"].geom_type == "Point"))
    pr = pd.DataFrame(pairs)
    match = {}      # os index -> dict(osm=index, method=..., ncand=...)
    if len(pr):
        pr["mrank"] = pr.method.map({"overlap": 0, "proximity": 1})
        # an OSM point belongs to its single nearest OS polygon only
        pts = pr[pr.osm_point].sort_values(["osm", "mrank", "dist"]).drop_duplicates("osm")
        pr = pd.concat([pr[~pr.osm_point], pts])
        pr = pr.sort_values(["os", "mrank", "same_type", "osm_area"], ascending=[True, True, False, True])
        for os_i, grp in pr.groupby("os"):
            best = grp.iloc[0]
            tied = grp[(grp.mrank == best.mrank)]
            # only a real ambiguity if the tied OSM candidates would classify the building differently
            kinds = {osm.at[int(o), "rule_ref"] for o in tied.osm}
            match[os_i] = dict(osm=int(best.osm), method=best.method, ncand=len(kinds))
    used_osm = {m["osm"] for m in match.values()}

    recs = []

    def winner(a, b):
        """Choose which source's rule wins: lowest Priority, then importance, then the OS rule."""
        ka = (prio.get(a.rule_ref, 50), IMP_RANK[a.importance])
        kb = (prio.get(b.rule_ref, 50), IMP_RANK[b.importance])
        return b if kb < ka else a

    for i, r in os_.iterrows():
        m = match.get(i)
        if m is None:
            recs.append(dict(source="OS Important Buildings", source_feature_id=r.source_id, name=r["name"],
                             base=r, os_ref=r.rule_ref, osm_ref=None, method="OS only", confidence="Medium",
                             review="", geometry=r.geometry, refs=r.matched_refs, osm_building=""))
            continue
        o = osm.loc[m["osm"]]
        w = winner(r, o)
        reasons = []
        expected_prox = m["method"] == "proximity" and r.building_type in _c().point_distance_by_type
        names_agree = name_similarity(r["name"], o["name"]) >= _c().name_agree if (r["name"] and o["name"]) else False
        if m["method"] == "proximity" and not expected_prox and not names_agree:
            reasons.append("Proximity-only match (close by, no overlap, names differ or missing)")
        if m["ncand"] > 1:
            reasons.append(f"{m['ncand']} OSM candidates for this OS building")
        if r.rule_ref != o.rule_ref:
            reasons.append(f"OS rule {r.rule_ref} ({r.building_type}) vs OSM rule {o.rule_ref} ({o.building_type})")
        recs.append(dict(source="Both", source_feature_id=f"{r.source_id} / {o.source_id}",
                         name=r["name"] or o["name"], base=w, os_ref=r.rule_ref, osm_ref=o.rule_ref,
                         method="Overlap" if m["method"] == "overlap" else ("Proximity (names agree)" if names_agree else "Proximity"),
                         confidence="High" if m["method"] == "overlap" else ("Medium" if (expected_prox or names_agree) else "Low"),
                         review="; ".join(reasons), osm_building=o.osm_building,
                         geometry=o.geometry if (r.geometry.geom_type == "Point" and o.geometry.geom_type != "Point")
                         else r.geometry,
                         refs=";".join(sorted(set((r.matched_refs + ";" + o.matched_refs).split(";")), key=int))))
    for j, o in osm.iterrows():
        if j in used_osm:
            continue
        is_area = o.note.startswith("Area feature")      # a site area with no buildings found inside: expected, not a doubt
        recs.append(dict(source="OSM", source_feature_id=o.source_id, name=o["name"], base=o, os_ref=None,
                         osm_ref=o.rule_ref, method="OSM area (no buildings)" if is_area else "OSM only",
                         confidence="Medium", review="",
                         osm_building=o.osm_building,
                         geometry=o.geometry, refs=o.matched_refs))
    if not recs:                      # nothing classified in this area: return an empty table with the usual columns
        return gpd.GeoDataFrame({c: [] for c in MERGED_COLUMNS}, geometry="geometry", crs=27700)
    out = pd.DataFrame(recs)
    b = out.pop("base")
    out["category"] = [x.category for x in b]
    out["building_type"] = [x.building_type for x in b]
    out["importance"] = [x.importance for x in b]
    out["driver"] = [x.driver for x in b]
    out["rule_ref"] = [x.rule_ref for x in b]
    out["rule_notes"] = [x.rule_notes for x in b]
    return gpd.GeoDataFrame(out, geometry="geometry", crs=27700)


# ====================================================================================================
# Name matching (shared by the merge, site and register-matching stages)
# ====================================================================================================

def _tokens(name):
    if name is None or (isinstance(name, float) and name != name):      # None / NaN: no name, no tokens
        return []
    s = re.sub(r"[^a-z0-9 ]", " ", str(name).lower().replace("&", " and ").replace("st.", "saint").replace("st ", "saint "))
    toks = [t for t in s.split() if t]
    keep = [t for t in toks if t not in config.get().reference_match.stop_words]
    return keep or toks


def strict_similarity(a, b):
    """Full-name similarity (no stop-word removal): 'Nayland Surgery' and 'Nayland House Care Home' are different."""
    na = re.sub(r"[^a-z0-9]", "", str(a).lower().replace("&", "and"))
    nb = re.sub(r"[^a-z0-9]", "", str(b).lower().replace("&", "and"))
    return SequenceMatcher(None, na, nb).ratio() if na and nb else 0.0


def name_similarity(a, b):
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    sa, sb = set(ta), set(tb)
    contain = len(sa & sb) / min(len(sa), len(sb))
    jacc = len(sa & sb) / len(sa | sb)
    seq = SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio()
    return max(contain * 0.9, jacc, seq)
