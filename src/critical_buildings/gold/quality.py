"""Data quality score (0-100) and band. Points and band edges come from config/pipeline.yml (quality section)."""
import numpy as np
import pandas as pd

from critical_buildings import config


def bands_label():
    q = config.get().quality
    return f"High {q.band_high_min}-100, Medium {q.band_medium_min}-{q.band_high_min - 1}, Low 0-{q.band_medium_min - 1}"


def quality_score(df):
    """0-100 score from how well a record is evidenced. A heuristic to help sorting and filtering, not a probability."""
    q = config.get().quality
    ver = df["Verification"].map(q.verification_points).fillna(10)
    src = df["Source"].map(q.source_points).fillna(q.register_points)
    conf = df["Confidence"].map(q.confidence_points).fillna(12)
    geom = np.where(df["Geom"].str.upper().str.startswith("POINT"), q.point_points, q.polygon_points)
    pen = np.where(df["Review Status"] == "Open", q.open_review_penalty, 0)
    return (ver + src + conf + geom + pen).clip(0, 100).astype(int)


def quality_band(score):
    q = config.get().quality
    return pd.cut(score, [-1, q.band_medium_min - 1, q.band_high_min - 1, 100], labels=["Low", "Medium", "High"]).astype(str)
