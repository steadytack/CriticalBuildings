"""OS National Grid 100 km squares (TM, TL, TQ ...): which square a British National Grid coordinate is in, and a square's extent.

The property table is built one square at a time, so that no step ever holds more than one square's UPRNs and buildings (the
largest, TQ around London, has a few million of each), and the OS Open Map Local data is already supplied per square.
"""
import numpy as np


def _letters(e100, n100):
    """The two grid letters of the 100 km square whose south-west corner is at (e100, n100) hundreds of kilometres."""
    first = (19 - n100) - (19 - n100) % 5 + (e100 + 10) // 5
    second = (19 - n100) * 5 % 25 + e100 % 5
    first += first > 7                    # the grid has no letter I
    second += second > 7
    return chr(first + 65) + chr(second + 65)


TILES = {(e, n): _letters(e, n) for e in range(7) for n in range(13)}       # (east, north) index -> 'TM'
INDEX = {code: en for en, code in TILES.items()}
_CODE_AT = np.full((7, 13), "", dtype="<U2")
for (_e, _n), _code in TILES.items():
    _CODE_AT[_e, _n] = _code


def tile_of(x, y):
    """The grid square code of each coordinate; '' for a coordinate outside the grid."""
    e = np.floor(np.asarray(x, dtype=float) / 100_000).astype(int)
    n = np.floor(np.asarray(y, dtype=float) / 100_000).astype(int)
    inside = (e >= 0) & (e < 7) & (n >= 0) & (n < 13)
    out = np.full(e.shape, "", dtype="<U2")
    out[inside] = _CODE_AT[e[inside], n[inside]]
    return out


def extent(code):
    """(min easting, min northing, max easting, max northing) of a grid square."""
    e, n = INDEX[code.upper()]
    return e * 100_000, n * 100_000, (e + 1) * 100_000, (n + 1) * 100_000
