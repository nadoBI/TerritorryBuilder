from __future__ import annotations
from typing import Dict, List, Tuple, Optional
from core.normalize import normalize_text

def extract_admin_index(admin_geojson: dict,
                        id_prop: str = "territory_id",
                        name_prop: str = "name") -> Tuple[Dict[str, dict], Dict[str, str]]:
    """
    Returns:
      by_id: territory_id -> feature
      norm_name_to_id: normalized name -> territory_id
    """
    by_id: Dict[str, dict] = {}
    norm_name_to_id: Dict[str, str] = {}

    features = admin_geojson.get("features", []) or []
    for f in features:
        props = f.get("properties", {}) or {}
        tid = str(props.get(id_prop, "")).strip()
        name = str(props.get(name_prop, "")).strip()
        if not tid and name:
            tid = normalize_text(name)
        if tid:
            by_id[tid] = f
        if name:
            norm_name_to_id[normalize_text(name)] = tid

    return by_id, norm_name_to_id

def feature_tooltip_html(props: dict) -> str:
    name = props.get("name", "")
    tid = props.get("territory_id", "")
    return f"{name} ({tid})"

def feature_popup_html(props: dict) -> str:
    # keep simple
    rows = []
    for k in ["name", "territory_id", "region", "type"]:
        if k in props and props.get(k) not in (None, ""):
            rows.append(f"<b>{k}</b>: {props.get(k)}")
    return "<br/>".join(rows) if rows else "Admin unit"
