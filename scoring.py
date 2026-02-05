from __future__ import annotations
from typing import Dict, List
import pandas as pd

def compute_total_weight(selected_admin_ids: List[str], weights_df: pd.DataFrame) -> float:
    if not selected_admin_ids or weights_df.empty:
        return 0.0
    w = weights_df[weights_df["territory_id"].astype(str).isin([str(x) for x in selected_admin_ids])]
    return float(w["weight"].sum())

def compute_weights_share(weights_df: pd.DataFrame) -> pd.DataFrame:
    df = weights_df.copy()
    total = df["weight"].sum()
    if total and total != 0:
        df["weight_share"] = df["weight"] / total
    else:
        df["weight_share"] = 0.0
    return df
