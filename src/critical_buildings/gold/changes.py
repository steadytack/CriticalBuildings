"""Change tracking between two runs."""
import pandas as pd


def diff_runs(prev, cur):
    """Compare two runs by Building ID. Returns a DataFrame of added / removed / reclassified / source changes."""
    p = prev.set_index("Building ID")
    c = cur.set_index("Building ID")
    rows = []
    for b in sorted(set(c.index) - set(p.index)):
        rows.append(("Added", b, c.at[b, "Building Type Default"], "", c.at[b, "Source Feature ID"]))
    for b in sorted(set(p.index) - set(c.index)):
        rows.append(("Removed", b, p.at[b, "Building Type Default"], "", p.at[b, "Source Feature ID"]))
    for b in sorted(set(p.index) & set(c.index)):
        if p.at[b, "Building Type Default"] != c.at[b, "Building Type Default"]:
            rows.append(("Reclassified", b, p.at[b, "Building Type Default"], c.at[b, "Building Type Default"],
                         c.at[b, "Source Feature ID"]))
        elif p.at[b, "Source"] != c.at[b, "Source"]:
            rows.append(("Source changed", b, p.at[b, "Source"], c.at[b, "Source"], c.at[b, "Source Feature ID"]))
    return pd.DataFrame(rows, columns=["Change", "Building ID", "Before / Type", "After", "Source Feature ID"])
