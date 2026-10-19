"""One function per kind of link from a UPRN to another dataset. Each takes coordinate arrays and returns arrays, and nothing else
about the pipeline, so each can be tested on a handful of made-up points.

Conventions: x / y are British National Grid metres, lon / lat are WGS84 degrees (planning.data.gov.uk publishes WGS84 outlines, and
OS Open UPRN supplies both, so a point is tested against them without reprojecting millions of polygon vertices).
"""
import numpy as np
import pyarrow as pa
import shapely
from shapely import STRtree


def first_hits(point_idx, geom_idx, n_points):
    """For each point, the lowest-numbered geometry that contains it (-1 if none). The lowest, so a point in two overlapping
    geometries is given the same one on every run, whatever order the spatial index returns them in."""
    out = np.full(n_points, -1, dtype=np.int64)
    if len(point_idx):
        order = np.lexsort((geom_idx, point_idx))
        p, g = point_idx[order], geom_idx[order]
        first = np.unique(p, return_index=True)[1]
        out[p[first]] = g[first]
    return out


def inside(x, y, geoms):
    """Index of the first geometry each point is inside or on (-1 for none).

    Two steps, not the index's own predicate option: the index finds the shapes whose bounding box holds the point, then the exact
    test runs on those pairs with each shape prepared. The one-step form re-examined a flood outline of 200,000 vertices for every
    point (47 seconds for 100,000 points); this takes a fraction of a second, with identical answers."""
    n = len(x)
    if not len(geoms):
        return np.full(n, -1, dtype=np.int64)
    geoms = np.asarray(geoms, dtype=object)
    shapely.prepare(geoms)
    points = shapely.points(x, y)
    pi, gi = STRtree(geoms).query(points)
    if len(pi):
        hit = shapely.intersects(geoms[gi], points[pi])             # the shape first: that is the argument the prepared index serves
        pi, gi = pi[hit], gi[hit]
    return first_hits(pi, gi, n)


def nearest(x, y, geoms, max_m):
    """(index of the nearest geometry, distance in metres) for each point; (-1, nan) when none is within max_m. Ties go to the
    lowest-numbered geometry, so the answer does not depend on the index's internal order."""
    n = len(x)
    idx, dist = np.full(n, -1, dtype=np.int64), np.full(n, np.nan)
    if not len(geoms) or not n:
        return idx, dist
    tree = STRtree(np.asarray(geoms, dtype=object))
    (pi, gi), d = tree.query_nearest(shapely.points(x, y), max_distance=max_m, return_distance=True, all_matches=True)
    if len(pi):
        order = np.lexsort((gi, d, pi))
        pi, gi, d = pi[order], gi[order], d[order]
        first = np.unique(pi, return_index=True)[1]
        idx[pi[first]], dist[pi[first]] = gi[first], d[first]
    return idx, dist


def coded(indices, labels):
    """A text column as a dictionary-coded Arrow array: `labels` is the small list of distinct values and `indices` says which one
    each row has (-1 = null). A column of 4 million repeated strings then costs a few megabytes."""
    indices = np.asarray(indices)
    mask = indices < 0
    return pa.DictionaryArray.from_arrays(pa.array(np.where(mask, 0, indices).astype(np.int32), mask=mask),
                                          pa.array(list(labels), type=pa.string()))


def lookup(indices, values, dtype=None):
    """values[index] per row as an Arrow array, null where the index is -1 (or the value is NaN)."""
    indices = np.asarray(indices)
    mask = indices < 0
    values = np.asarray(values)
    if not len(values):
        return pa.nulls(len(indices), type=dtype or pa.null())
    taken = values[np.where(mask, 0, indices)]
    if np.issubdtype(taken.dtype, np.floating):
        mask = mask | np.isnan(taken)
        taken = np.where(mask, 0, taken)
        if dtype is not None and pa.types.is_integer(dtype):
            taken = taken.astype(np.int64)
    return pa.array(taken, type=dtype, mask=mask)


def sorted_index(keys, sorted_keys):
    """For each of `keys`, its position in `sorted_keys` (ascending, unique), or -1 when it is not there."""
    sorted_keys = np.asarray(sorted_keys)
    if not len(sorted_keys):
        return np.full(len(keys), -1)
    pos = np.searchsorted(sorted_keys, keys)
    pos = np.minimum(pos, len(sorted_keys) - 1)
    return np.where(sorted_keys[pos] == keys, pos, -1)


# ====================================================================================================
# Building: which footprint a UPRN is inside, and how many UPRNs share it
# ====================================================================================================

def building_link(x, y, footprints):
    """(building index per UPRN or -1, footprint area per UPRN, UPRNs in that building per UPRN)."""
    b = inside(x, y, footprints)
    areas = shapely.area(np.asarray(footprints, dtype=object)) if len(footprints) else np.array([])
    per_building = np.bincount(b[b >= 0], minlength=len(footprints)) if len(footprints) else np.array([], dtype=int)
    area = np.where(b >= 0, areas[np.where(b >= 0, b, 0)] if len(areas) else np.nan, np.nan)
    count = np.where(b >= 0, per_building[np.where(b >= 0, b, 0)] if len(per_building) else 0, 0)
    return b, area.astype(float), count.astype(np.int32)


# ====================================================================================================
# Postcode: the nearest Code-Point Open postcode point
# ====================================================================================================

class PostcodeIndex:
    """All Code-Point Open postcode points, in a spatial index built once."""

    def __init__(self, table):
        """table: indexed by normalised postcode, with EA / NO (easting / northing) columns, as silver._postcode_table() gives."""
        order = np.argsort(table.index.to_numpy().astype(str), kind="stable")       # fixed order, so ties are settled the same way
        self.names = table.index.to_numpy().astype(str)[order]
        self.points = shapely.points(table["EA"].to_numpy(float)[order], table["NO"].to_numpy(float)[order])
        self.tree = STRtree(self.points)

    def nearest(self, x, y, max_m):
        """(nearest postcode in display form or '' per point, distance in metres or nan)."""
        n = len(x)
        idx, dist = np.full(n, -1, dtype=np.int64), np.full(n, np.nan)
        (pi, gi), d = self.tree.query_nearest(shapely.points(x, y), max_distance=max_m, return_distance=True, all_matches=True)
        if len(pi):
            order = np.lexsort((gi, d, pi))
            pi, gi, d = pi[order], gi[order], d[order]
            first = np.unique(pi, return_index=True)[1]
            idx[pi[first]], dist[pi[first]] = gi[first], d[first]
        return idx, dist

    def display(self, idx):
        """The postcode names for an index array, as an Arrow column ('IP11AA' -> 'IP1 1AA'), null where idx is -1."""
        used = np.unique(idx[idx >= 0])
        labels = [f"{p[:-3]} {p[-3:]}" if len(p) > 3 else p for p in self.names[used]]
        position = np.full(len(self.names), -1, dtype=np.int64)
        position[used] = np.arange(len(used))
        return coded(np.where(idx >= 0, position[np.where(idx >= 0, idx, 0)], -1), labels)


# ====================================================================================================
# Planning and flood layers, tested in WGS84
# ====================================================================================================

def flood_zone(lon, lat, zone3, zone2):
    """(zone 3 / 2 / 1 per point, index into the winning layer's polygons or -1). zone3 / zone2 are the polygon arrays of each level."""
    in3 = inside(lon, lat, zone3)
    in2 = inside(lon, lat, zone2)
    zone = np.where(in3 >= 0, 3, np.where(in2 >= 0, 2, 1)).astype(np.int8)
    return zone, in3, in2
