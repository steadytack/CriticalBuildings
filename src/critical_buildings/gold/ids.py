"""Stable identities: Building IDs and Site IDs survive reruns. Registries live in the control store (append-only)."""
import geopandas as gpd
import pandas as pd


def assign_ids(recs, today, store, run_id):
    """Keep Building IDs stable across runs (match on OS id, then OSM id, then footprint overlap)."""
    recs = recs.copy()
    recs["os_key"] = recs.source_feature_id.str.split(" / ").str[0].where(recs.source != "OSM")
    recs["osm_key"] = recs.source_feature_id.str.split(" / ").str[-1].where(recs.source != "OS Important Buildings")
    reg = store.building_registry_df()
    by_os = {k: b for k, b in zip(reg.os_key, reg.building_id) if isinstance(k, str)}
    osm_only = reg[reg.os_key.isna()]
    by_osm = {k: b for k, b in zip(osm_only.osm_key, osm_only.building_id) if isinstance(k, str)}
    created = dict(zip(reg.building_id, reg.created))
    nxt = (reg.building_id.str[5:].astype(int).max() + 1) if len(reg) else 1
    ids = []
    taken = set()
    for _, r in recs.iterrows():
        b = by_os.get(r.os_key) if isinstance(r.os_key, str) else None
        if b is None and isinstance(r.osm_key, str) and r.source == "OSM":
            b = by_osm.get(r.osm_key)
        if b is None and isinstance(r.osm_key, str) and r.source == "Both":
            b = by_osm.get(r.osm_key)          # was OSM-only last run, now also matched to OS
        if b in taken:
            b = None
        ids.append(b)
        if b:
            taken.add(b)
    # footprint-overlap fallback for the rest (e.g. OSM feature redrawn under a new id)
    if len(reg) and any(i is None for i in ids):
        old = gpd.GeoSeries(gpd.GeoSeries.from_wkt(reg.wkt), crs=27700)
        for n, (i, r) in enumerate(zip(ids, recs.itertuples())):
            if i is not None:
                continue
            hit = old[old.intersects(r.geometry)]
            for k in hit.index:
                cand = reg.at[k, "building_id"]
                if cand in taken:
                    continue
                ov = old[k].intersection(r.geometry).area / max(min(old[k].area, r.geometry.area), 1e-9)
                if ov >= 0.5:
                    ids[n] = cand
                    taken.add(cand)
                    break
    new = 0
    for n, i in enumerate(ids):
        if i is None:
            ids[n] = f"BLDG-{nxt:06d}"
            nxt += 1
            new += 1
        created.setdefault(ids[n], str(today))
    recs["Building ID"] = ids
    recs["Created Date"] = recs["Building ID"].map(created)
    store.save_building_registry(pd.DataFrame(dict(building_id=recs["Building ID"], os_key=recs.os_key,
                                                   osm_key=recs.osm_key, created=recs["Created Date"],
                                                   wkt=recs.geometry.to_wkt())), run_id)
    return recs, new


def assign_site_ids(recs, sites, today, store, run_id):
    """Stable Site IDs: a site keeps the ID most of its buildings were last assigned (remembered in the registry)."""
    old_site = store.building_site_map()
    reg = store.site_registry_df()
    created = dict(zip(reg.site_id, reg.created))
    nxt = (reg.site_id.str[5:].astype(int).max() + 1) if len(reg) else 1
    taken, mapping = set(), {}
    for g, n in sites.sort_values("n_buildings", ascending=False)[["site_group", "n_buildings"]].itertuples(index=False):
        votes = pd.Series([old_site.get(b) for b in recs.loc[recs.site_group == g, "Building ID"]]).dropna()
        sid = next((v for v in votes.value_counts().index if v not in taken), None)
        if sid is None:
            sid = f"SITE-{nxt:06d}"
            nxt += 1
        taken.add(sid)
        mapping[g] = sid
        created.setdefault(sid, str(today))
    recs = recs.copy()
    recs["Site ID"] = recs.site_group.map(mapping)
    sites = sites.copy()
    sites["Site ID"] = sites.site_group.map(mapping)
    sites["Created Date"] = sites["Site ID"].map(created)
    store.save_site_registry({sid: created[sid] for sid in mapping.values()}, run_id)
    store.set_building_sites(dict(zip(recs["Building ID"], recs["Site ID"])))
    return recs, sites
