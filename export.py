from __future__ import annotations
from typing import Dict, List, Tuple
import io
import pandas as pd
import folium

from core.scoring import compute_total_weight

def build_allocations_table(project,
                            weights_df: pd.DataFrame,
                            business_line: str = "",
                            job_title: str = "") -> pd.DataFrame:
    rows = []
    for t in project.territories:
        w = compute_total_weight(t.admin_unit_ids, weights_df)
        for admin_id in (t.admin_unit_ids or [""]):
            rows.append({
                "BusinessLine": business_line,
                "JobTitle": job_title,
                "CommercialTerritory": t.territory_name,
                "Rep_User_Id": t.rep_user_id,
                "AM_User_Id": t.am_user_id,
                "Admin_Territory_Id": admin_id,
                "TerritoryWeight": w,
            })
    return pd.DataFrame(rows)

def summary_pivot(project, weights_df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for t in project.territories:
        rows.append({
            "CommercialTerritory": t.territory_name,
            "Rep_User_Id": t.rep_user_id,
            "AM_User_Id": t.am_user_id,
            "Weight": compute_total_weight(t.admin_unit_ids, weights_df),
            "AdminUnits": len(t.admin_unit_ids or []),
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    # simple pivot-like summary
    df = df.sort_values(["AM_User_Id", "Rep_User_Id", "CommercialTerritory"], na_position="last")
    return df

def df_to_xlsx_bytes(sheets: Dict[str, pd.DataFrame]) -> bytes:
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        for name, df in sheets.items():
            df.to_excel(writer, sheet_name=name[:31] or "Sheet", index=False)
    return out.getvalue()

def save_map_html(m: folium.Map) -> bytes:
    html = m.get_root().render()
    return html.encode("utf-8")
