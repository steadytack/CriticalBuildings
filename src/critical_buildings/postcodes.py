"""OS Code-Point Open postcodes: one point per postcode (about 1.7 million), shared by every product."""
import functools
import zipfile

import pandas as pd

from critical_buildings import paths


def norm_pc(x):
    return str(x).replace(" ", "").upper() if isinstance(x, str) else ""


@functools.lru_cache(maxsize=1)
def postcode_table():
    """All Code-Point Open postcodes (about 1.7 million) as a table indexed by normalised postcode, columns EA / NO."""
    wanted = ("PC", "EA", "NO")          # postcode, easting, northing
    with zipfile.ZipFile(paths.bronze_file("os_codepoint_open", "codepo_gb.zip")) as z:
        cols = pd.read_csv(z.open("Doc/Code-Point_Open_Column_Headers.csv"), header=None).iloc[0].tolist()
        pos = [cols.index(c) for c in wanted]
        assert pos == sorted(pos), "Code-Point column order changed"        # the reader keeps file order
        frames = [pd.read_csv(z.open(n), header=None, names=list(wanted), usecols=pos, engine="pyarrow")
                  for n in z.namelist() if n.startswith("Data/CSV/") and n.endswith(".csv")]
    cp = pd.concat(frames)
    cp.index = cp["PC"].str.replace(" ", "", regex=False).str.upper()
    return cp[~cp.index.duplicated(keep="last")][["EA", "NO"]]


def postcode_points(wanted=None):
    """Postcode -> (easting, northing) from OS Code-Point Open, for the normalised postcodes in `wanted` (all if None)."""
    table = postcode_table()
    if wanted is not None:
        table = table[table.index.isin(list(wanted))]
    return dict(zip(table.index, zip(table["EA"], table["NO"], strict=False), strict=False))
