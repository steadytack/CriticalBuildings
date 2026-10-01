"""Apply human decisions (from the control store) to the assembled building table."""
OVR_COLS = ["Primary Category Override", "Building Type Override", "Operational Importance Override",
            "Operational Importance Driver Override", "Manual Override Notes", "Building Role Override",
            "Review Decision", "Verification Override"]


def apply_overrides(df, store):
    ov = store.overrides_df().fillna("")
    for c in OVR_COLS + ["Exclude (Y)", "Exclude Site (Y)"]:      # people may fill in only the columns they need
        if c not in ov:
            ov[c] = ""
    ov = ov[ov["Building ID"].isin(df["Building ID"])]
    ov = ov[["Building ID"] + OVR_COLS + ["Exclude (Y)", "Exclude Site (Y)"]].drop_duplicates("Building ID", keep="last")
    df = df.drop(columns=OVR_COLS).merge(ov, on="Building ID", how="left")
    for c in OVR_COLS:
        df[c] = df[c].fillna("")
    df["Excluded"] = df["Exclude (Y)"].fillna("").str.upper().eq("Y")
    # site-level decisions: marking any one building of a site applies to the whole site
    ex_sites = set(df.loc[df["Exclude Site (Y)"].fillna("").str.upper().eq("Y"), "Site ID"])
    df.loc[df["Site ID"].isin(ex_sites), "Excluded"] = True
    vo = df[df["Verification Override"] != ""].groupby("Site ID")["Verification Override"].first()
    df["Verification"] = df["Site ID"].map(vo).fillna(df["Verification"])
    return df.drop(columns=["Exclude (Y)", "Exclude Site (Y)"], errors="ignore")
