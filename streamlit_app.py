from __future__ import annotations

import json
import random
import re
from pathlib import Path
from typing import Dict, Optional, List

import pandas as pd
import streamlit as st
import folium
from folium.features import GeoJsonTooltip, GeoJsonPopup
from streamlit_folium import st_folium

from core.normalize import safe_email
from core.io import read_csv_any, validate_weights
from core.geo import extract_admin_index
from core.exports import df_to_xlsx_bytes, save_map_html  # if openpyxl missing, XLSX will be disabled

APP_TITLE = "Territory Allocation MVP (Pharma)"
AUTH_CSV_PATH = Path("data/auth/licensed_users.csv")

# Put your fixed GeoJSON in Git here:
ROM_GEO_PATH = Path("data/geo/Romania_Iqvia_sector&Judeti_0,012.geojson")

st.set_page_config(page_title=APP_TITLE, layout="wide")


# -----------------------------
# Auth
# -----------------------------
def load_auth_table() -> pd.DataFrame:
    if not AUTH_CSV_PATH.exists():
        return pd.DataFrame(columns=["email", "password", "active"])
    df = pd.read_csv(AUTH_CSV_PATH)
    for c in ["email", "password", "active"]:
        if c not in df.columns:
            df[c] = ""
    df["email"] = df["email"].astype(str).str.lower().str.strip()
    df["active"] = df["active"].astype(str).str.lower().isin(["true", "1", "yes", "y"])
    return df


def check_login(email: str, password: str) -> bool:
    df = load_auth_table()
    email = safe_email(email)
    match = df[
        (df["email"] == email)
        & (df["password"].astype(str) == str(password))
        & (df["active"] == True)
    ]
    return len(match) > 0


# -----------------------------
# Session defaults
# -----------------------------
def ensure_session_defaults():
    st.session_state.setdefault("logged_in", False)
    st.session_state.setdefault("user_email", "")

    # Inputs
    st.session_state.setdefault("weights_df", pd.DataFrame())
    st.session_state.setdefault("reps_df", pd.DataFrame())
    st.session_state.setdefault("ams_df", pd.DataFrame())

    # Geo / admin base
    st.session_state.setdefault("admin_geojson", None)
    st.session_state.setdefault("admin_by_id", {})          # territory_id -> feature
    st.session_state.setdefault("admin_units_df", pd.DataFrame())  # base admin units + weight

    # Assignment master table (truth)
    st.session_state.setdefault("assign_df", pd.DataFrame())  # 1 row per admin unit: rep/am assignments

    # UI state
    st.session_state.setdefault("selected_admin_ids", [])
    st.session_state.setdefault("last_popup_tid", None)
    st.session_state.setdefault("map_center", [45.94, 24.97])
    st.session_state.setdefault("map_zoom", 6)

    # Colors per REP
    st.session_state.setdefault("rep_color_map", {})


ensure_session_defaults()


def login_ui():
    st.title(APP_TITLE)
    st.caption("Simple CSV-based license check. No roles, no DB.")
    with st.form("login_form"):
        email = st.text_input("Email", value=st.session_state.get("user_email", ""))
        password = st.text_input("Password", type="password")
        ok = st.form_submit_button("Login")
    if ok:
        if check_login(email, password):
            st.session_state["logged_in"] = True
            st.session_state["user_email"] = safe_email(email)
            st.success("Logged in.")
            st.rerun()
        else:
            st.error("Invalid credentials or inactive license.")


if not st.session_state["logged_in"]:
    login_ui()
    st.stop()


# -----------------------------
# Helpers (people + assignments)
# -----------------------------
def _full_name(df: pd.DataFrame) -> pd.Series:
    return (
        df["Name"].astype(str).fillna("").str.strip()
        + " "
        + df["Surname"].astype(str).fillna("").str.strip()
    ).str.strip()


def validate_people_df(df: pd.DataFrame, label: str) -> pd.DataFrame:
    required = ["User_Id", "Name", "Surname", "Email", "Lat", "Long", "BusinessLine"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        st.error(f"{label}: missing columns {missing}")
        return pd.DataFrame()

    df = df.copy()
    df["User_Id"] = df["User_Id"].astype(str).str.strip()
    df["Email"] = df["Email"].astype(str).str.strip().str.lower()
    df["Lat"] = pd.to_numeric(df["Lat"], errors="coerce")
    df["Long"] = pd.to_numeric(df["Long"], errors="coerce")
    df["BusinessLine"] = df["BusinessLine"].astype(str).str.strip()

    df = df[df["User_Id"] != ""]
    if df["User_Id"].duplicated().any():
        dups = df[df["User_Id"].duplicated()]["User_Id"].tolist()
        st.error(f"{label}: duplicated User_Id: {dups[:10]}")
        return pd.DataFrame()

    return df


def build_color_map_for_reps(reps_df: pd.DataFrame) -> Dict[str, str]:
    m: Dict[str, str] = {}
    for rid in reps_df["User_Id"].astype(str).str.strip().tolist():
        m[rid] = f"#{random.randint(0, 0xFFFFFF):06x}"
    return m


def build_admin_units_df(admin_by_id: Dict[str, dict], weights_df: pd.DataFrame) -> pd.DataFrame:
    # base admin units list (from GeoJSON)
    rows = []
    for tid, feat in admin_by_id.items():
        props = (feat or {}).get("properties", {}) or {}
        name = str(props.get("name", tid))
        rows.append({"territory_id": str(tid).strip(), "territory_name": name.strip()})
    df = pd.DataFrame(rows)

    # merge weights
    w = weights_df.copy()
    w["territory_id"] = w["territory_id"].astype(str).str.strip()
    w["weight"] = pd.to_numeric(w["weight"], errors="coerce").fillna(0.0)

    df = df.merge(w[["territory_id", "weight"]], on="territory_id", how="left")
    df["weight"] = df["weight"].fillna(0.0)
    return df


def build_assign_df(admin_units_df: pd.DataFrame, reps_df: pd.DataFrame, ams_df: pd.DataFrame) -> pd.DataFrame:
    df = admin_units_df.copy()
    df["territory_id"] = df["territory_id"].astype(str).str.strip()
    df["territory_name"] = df["territory_name"].astype(str).str.strip()
    df["weight"] = pd.to_numeric(df["weight"], errors="coerce").fillna(0.0)

    for c in ["rep_id", "rep_name", "am_id", "am_name"]:
        if c not in df.columns:
            df[c] = ""

    # names from lookups
    if not reps_df.empty:
        r = reps_df.copy()
        r["User_Id"] = r["User_Id"].astype(str).str.strip()
        r["rep_name"] = _full_name(r)
        rep_lookup = dict(zip(r["User_Id"], r["rep_name"]))
        df["rep_name"] = df["rep_id"].map(rep_lookup).fillna(df["rep_name"])

    if not ams_df.empty:
        a = ams_df.copy()
        a["User_Id"] = a["User_Id"].astype(str).str.strip()
        a["am_name"] = _full_name(a)
        am_lookup = dict(zip(a["User_Id"], a["am_name"]))
        df["am_name"] = df["am_id"].map(am_lookup).fillna(df["am_name"])

    keep = ["territory_id", "territory_name", "weight", "rep_id", "rep_name", "am_id", "am_name"]
    return df[keep]


def apply_assoc_to_assign(assign_df: pd.DataFrame, assoc_df: pd.DataFrame) -> pd.DataFrame:
    df = assign_df.copy()
    a = assoc_df.copy()
    for c in ["territory_id", "rep_id", "am_id"]:
        if c in a.columns:
            a[c] = a[c].astype(str).str.strip()

    if "territory_id" not in a.columns:
        st.error("Association CSV must contain territory_id")
        return df

    cols = ["territory_id"] + [c for c in ["rep_id", "am_id"] if c in a.columns]
    a = a[cols].drop_duplicates(subset=["territory_id"])

    tmp = df.merge(a, on="territory_id", how="left", suffixes=("", "_new"))
    if "rep_id_new" in tmp.columns:
        tmp["rep_id"] = tmp["rep_id_new"].fillna(tmp["rep_id"])
        tmp.drop(columns=["rep_id_new"], inplace=True)
    if "am_id_new" in tmp.columns:
        tmp["am_id"] = tmp["am_id_new"].fillna(tmp["am_id"])
        tmp.drop(columns=["am_id_new"], inplace=True)

    return tmp


def _parse_tid_from_popup(popup_html: str) -> Optional[str]:
    if not popup_html:
        return None
    m1 = re.search(r"(RO[_\-][A-Za-z0-9_\-]+)", popup_html)
    if m1:
        return m1.group(1)
    m2 = re.search(r"([A-Za-z0-9_\-]{3,})", popup_html)
    return m2.group(1) if m2 else None


# -----------------------------
# Load fixed GeoJSON from repo
# -----------------------------
if st.session_state["admin_geojson"] is None and ROM_GEO_PATH.exists():
    st.session_state["admin_geojson"] = json.loads(ROM_GEO_PATH.read_text(encoding="utf-8"))
    by_id, _ = extract_admin_index(st.session_state["admin_geojson"], id_prop="territory_id", name_prop="name")
    st.session_state["admin_by_id"] = by_id


# -----------------------------
# Header
# -----------------------------
st.title(APP_TITLE)
colA, colB, colC = st.columns([2, 1, 1])
with colA:
    st.write(f"**User:** {st.session_state['user_email']}")
with colB:
    if st.button("Logout"):
        st.session_state["logged_in"] = False
        st.rerun()
with colC:
    st.caption("GeoJSON is fixed in repo (no upload).")

st.divider()


# -----------------------------
# Sidebar: Save / Open project JSON (new format)
# -----------------------------
def export_project_json() -> bytes:
    payload = {
        "meta": {
            "app": APP_TITLE,
            "country": "Romania",
            "level": "Judete+BucharestSectors",
        },
        "reps": st.session_state["reps_df"].to_dict(orient="records"),
        "ams": st.session_state["ams_df"].to_dict(orient="records"),
        "rep_color_map": st.session_state["rep_color_map"],
        "assignments": st.session_state["assign_df"].to_dict(orient="records"),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def load_project_json(s: str) -> None:
    obj = json.loads(s)

    reps = pd.DataFrame(obj.get("reps", []))
    ams = pd.DataFrame(obj.get("ams", []))
    assign = pd.DataFrame(obj.get("assignments", []))

    st.session_state["reps_df"] = reps if not reps.empty else pd.DataFrame()
    st.session_state["ams_df"] = ams if not ams.empty else pd.DataFrame()
    st.session_state["rep_color_map"] = obj.get("rep_color_map", {}) or {}

    # rebuild assign_df safely using current admin_units_df (territory list)
    if not st.session_state["admin_units_df"].empty and not assign.empty:
        # keep only relevant columns
        cols = ["territory_id", "rep_id", "am_id"]
        keep = [c for c in cols if c in assign.columns]
        assoc = assign[keep].copy()
        base = build_assign_df(st.session_state["admin_units_df"], st.session_state["reps_df"], st.session_state["ams_df"])
        base = apply_assoc_to_assign(base, assoc.rename(columns={"rep_id": "rep_id", "am_id": "am_id"}))
        st.session_state["assign_df"] = build_assign_df(base, st.session_state["reps_df"], st.session_state["ams_df"])
    else:
        st.session_state["assign_df"] = build_assign_df(st.session_state["admin_units_df"], st.session_state["reps_df"], st.session_state["ams_df"])

    st.session_state["selected_admin_ids"] = []
    st.session_state["last_popup_tid"] = None


with st.sidebar:
    st.header("Project")
    save_name = st.text_input("Save filename", value="project_romania.json")
    st.download_button(
        "Download project JSON",
        data=export_project_json(),
        file_name=save_name,
        mime="application/json",
        disabled=st.session_state["assign_df"].empty,
    )

    uploaded_project = st.file_uploader("Open project JSON", type=["json"])
    if uploaded_project is not None:
        try:
            s = uploaded_project.read().decode("utf-8")
            load_project_json(s)
            st.success("Project loaded.")
            st.rerun()
        except Exception as e:
            st.error(f"Could not load project: {e}")


# -----------------------------
# Data loaders
# -----------------------------
st.subheader("1) Load data (CSV)")

# Download templates
t1, t2, t3, t4 = st.columns(4)
with t1:
    st.download_button(
        "Template REPs",
        data=Path("data/templates/reps_template.csv").read_bytes() if Path("data/templates/reps_template.csv").exists() else b"",
        file_name="reps_template.csv",
    )
with t2:
    st.download_button(
        "Template AMs",
        data=Path("data/templates/ams_template.csv").read_bytes() if Path("data/templates/ams_template.csv").exists() else b"",
        file_name="ams_template.csv",
    )
with t3:
    st.download_button(
        "Template Weights",
        data=Path("data/templates/weights_template.csv").read_bytes() if Path("data/templates/weights_template.csv").exists() else b"",
        file_name="weights_template.csv",
    )
with t4:
    st.download_button(
        "Template Assoc (optional)",
        data=Path("data/templates/assoc_template.csv").read_bytes() if Path("data/templates/assoc_template.csv").exists() else b"",
        file_name="assoc_template.csv",
    )

c1, c2, c3, c4 = st.columns(4)
with c1:
    up_weights = st.file_uploader("Weights CSV", type=["csv"], key="weights_upl")
with c2:
    up_reps = st.file_uploader("REPs CSV", type=["csv"], key="reps_upl")
with c3:
    up_ams = st.file_uploader("AMs CSV", type=["csv"], key="ams_upl")
with c4:
    up_assoc = st.file_uploader("Association CSV (optional)", type=["csv"], key="assoc_upl")

issues: List[str] = []

if up_weights is not None:
    wdf = read_csv_any(up_weights)
    wdf, w_issues = validate_weights(wdf)
    st.session_state["weights_df"] = wdf
    if w_issues:
        issues.append(f"Weights issues: {w_issues}")

if up_reps is not None:
    reps = read_csv_any(up_reps)
    reps = validate_people_df(reps, "REPs")
    st.session_state["reps_df"] = reps
    if not reps.empty:
        st.session_state["rep_color_map"] = build_color_map_for_reps(reps)

if up_ams is not None:
    ams = read_csv_any(up_ams)
    ams = validate_people_df(ams, "AMs")
    st.session_state["ams_df"] = ams

if issues:
    st.warning(" | ".join(issues))


# -----------------------------
# Build base admin_units_df and assign_df
# -----------------------------
if st.session_state["admin_units_df"].empty and st.session_state["admin_by_id"] and not st.session_state["weights_df"].empty:
    st.session_state["admin_units_df"] = build_admin_units_df(st.session_state["admin_by_id"], st.session_state["weights_df"])

# initialize assign_df if possible
if not st.session_state["admin_units_df"].empty and st.session_state["assign_df"].empty:
    st.session_state["assign_df"] = build_assign_df(st.session_state["admin_units_df"], st.session_state["reps_df"], st.session_state["ams_df"])

# apply association if uploaded (requires assign_df exists)
if up_assoc is not None and not st.session_state["assign_df"].empty:
    assoc = read_csv_any(up_assoc)
    st.session_state["assign_df"] = apply_assoc_to_assign(st.session_state["assign_df"], assoc)
    st.session_state["assign_df"] = build_assign_df(st.session_state["assign_df"], st.session_state["reps_df"], st.session_state["ams_df"])
    st.success("Associations applied to assignment table.")


with st.expander("Loaded data preview"):
    st.write("**Weights**")
    st.dataframe(st.session_state["weights_df"].head(50), use_container_width=True)
    st.write("**REPs**")
    st.dataframe(st.session_state["reps_df"].head(50), use_container_width=True)
    st.write("**AMs**")
    st.dataframe(st.session_state["ams_df"].head(50), use_container_width=True)
    st.write("**Admin units**:", len(st.session_state["admin_units_df"]))
    st.write("**Assignments**:", len(st.session_state["assign_df"]))


st.divider()


# -----------------------------
# Map
# -----------------------------
@st.cache_data(show_spinner=False)
def cached_admin_geojson(admin_geojson: dict) -> dict:
    return admin_geojson


def _icon_html(color: str, kind: str) -> str:
    # small dot marker; different shape by kind using border
    border = "2px solid #000000" if kind == "REP" else "2px dashed #000000"
    return f"""
    <div style="
        width:12px;height:12px;border-radius:50%;
        background:{color};
        border:{border};
        opacity:0.9;">
    </div>
    """


def make_map(show_admin_layer: bool = True, show_assigned_colors: bool = True) -> folium.Map:
    m = folium.Map(
        location=st.session_state["map_center"],
        zoom_start=st.session_state["map_zoom"],
        control_scale=True,
        tiles="OpenStreetMap",
    )

    if st.session_state["admin_geojson"] is None:
        folium.LayerControl().add_to(m)
        return m

    admin_geojson = cached_admin_geojson(st.session_state["admin_geojson"])
    assign_df = st.session_state["assign_df"]
    rep_color_map = st.session_state["rep_color_map"] or {}

    # Build territory_id -> rep_id and -> color
    tid_to_rep: Dict[str, str] = {}
    tid_to_color: Dict[str, str] = {}
    if not assign_df.empty:
        for _, r in assign_df.iterrows():
            tid = str(r.get("territory_id", "")).strip()
            rid = str(r.get("rep_id", "")).strip()
            if tid:
                tid_to_rep[tid] = rid
                if rid and rid in rep_color_map:
                    tid_to_color[tid] = rep_color_map[rid]

    selected_now = set(st.session_state.get("selected_admin_ids", []))

    def style_fn(feature):
        props = feature.get("properties", {}) or {}
        tid = str(props.get("territory_id", "")).strip()

        fill_color = "#cccccc"
        fill_opacity = 0.10
        weight = 1

        if show_assigned_colors and tid in tid_to_color:
            fill_color = tid_to_color[tid]
            fill_opacity = 0.22

        if tid in selected_now:
            fill_color = "#000000"
            fill_opacity = 0.45
            weight = 2

        return {
            "fillColor": fill_color,
            "color": fill_color,
            "fillOpacity": fill_opacity,
            "weight": weight,
        }

    admin_layer = folium.FeatureGroup(name="Admin units", show=show_admin_layer)
    folium.GeoJson(
        admin_geojson,
        name="Admin units",
        style_function=style_fn,
        tooltip=GeoJsonTooltip(fields=["name", "territory_id"], aliases=["Name", "ID"], sticky=True),
        popup=GeoJsonPopup(fields=["territory_id"], aliases=["ID"], labels=True),
    ).add_to(admin_layer)
    admin_layer.add_to(m)

    # People layers
    rep_layer = folium.FeatureGroup(name="REPs", show=True)
    am_layer = folium.FeatureGroup(name="AMs", show=False)

    def add_people(df: pd.DataFrame, layer: folium.FeatureGroup, kind: str):
        if df.empty:
            return
        for _, r in df.iterrows():
            lat, lon = r.get("Lat"), r.get("Long")
            if pd.isna(lat) or pd.isna(lon):
                continue

            uid = str(r.get("User_Id", "")).strip()
            nm = f"{r.get('Name','')} {r.get('Surname','')}".strip()
            bl = str(r.get("BusinessLine", "")).strip()
            prov = str(r.get("Province", "")).strip()

            tooltip = f"{kind}: {nm} | {bl} | {prov}"
            popup_html = (
                f"<b>{kind}: {nm}</b><br/>"
                f"User_Id: {uid}<br/>"
                f"BusinessLine: {bl}<br/>"
                f"Province: {prov}<br/>"
                f"Region: {str(r.get('Region','')).strip()}<br/>"
                f"Address: {str(r.get('Address','')).strip()}"
            )

            color = "#1f77b4" if kind == "AM" else rep_color_map.get(uid, "#ff7f0e")
            folium.Marker(
                location=[float(lat), float(lon)],
                icon=folium.DivIcon(html=_icon_html(color, kind)),
                tooltip=tooltip,
                popup=folium.Popup(popup_html, max_width=320),
            ).add_to(layer)

    add_people(st.session_state["reps_df"], rep_layer, "REP")
    add_people(st.session_state["ams_df"], am_layer, "AM")

    rep_layer.add_to(m)
    am_layer.add_to(m)

    folium.LayerControl().add_to(m)
    return m


# -----------------------------
# Assignment UI
# -----------------------------
st.subheader("2) Assign territories (REP / AM)")

if st.session_state["assign_df"].empty:
    st.info("Load weights + REPs + AMs to initialize assignment table.")
else:
    left, right = st.columns([1.2, 1])

    # --- LEFT: Map
    with left:
        st.write("### Map")
        m = make_map(show_admin_layer=True, show_assigned_colors=True)
        folium_state = st_folium(m, width=None, height=650, returned_objects=["last_object_clicked_popup"])
        popup = folium_state.get("last_object_clicked_popup")
        if popup:
            tid = _parse_tid_from_popup(str(popup))
            if tid and st.session_state.get("last_popup_tid") != tid:
                st.session_state["last_popup_tid"] = tid
                st.session_state["selected_admin_ids"] = [tid]  # single selection
                st.rerun()

    # --- RIGHT: Tables / assignment
    with right:
        st.write("### Assignment table")
        assign_df = st.session_state["assign_df"]
        reps_df = st.session_state["reps_df"]
        ams_df = st.session_state["ams_df"]

        selected_tid = (st.session_state.get("selected_admin_ids") or [None])[0]
        if selected_tid:
            row = assign_df[assign_df["territory_id"] == str(selected_tid)]
            if not row.empty:
                tn = row.iloc[0]["territory_name"]
                tw = float(row.iloc[0]["weight"])
                st.caption(f"Selected: **{tn}** ({tw:.2f})")

        rep_options = [""] + (
            (reps_df["User_Id"].astype(str).str.strip() + " - " + _full_name(reps_df)).tolist()
            if not reps_df.empty
            else []
        )
        am_options = [""] + (
            (ams_df["User_Id"].astype(str).str.strip() + " - " + _full_name(ams_df)).tolist()
            if not ams_df.empty
            else []
        )

        active_rep = st.selectbox("Active REP", rep_options)
        active_am = st.selectbox("Active AM", am_options)
        rep_id = active_rep.split(" - ")[0] if active_rep else ""
        am_id = active_am.split(" - ")[0] if active_am else ""

        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button("Assign selected -> REP", disabled=(not selected_tid or not rep_id)):
                df = st.session_state["assign_df"].copy()
                mask = df["territory_id"] == str(selected_tid)
                df.loc[mask, "rep_id"] = rep_id
                st.session_state["assign_df"] = build_assign_df(df, reps_df, ams_df)
                st.rerun()
        with c2:
            if st.button("Assign selected -> AM", disabled=(not selected_tid or not am_id)):
                df = st.session_state["assign_df"].copy()
                mask = df["territory_id"] == str(selected_tid)
                df.loc[mask, "am_id"] = am_id
                st.session_state["assign_df"] = build_assign_df(df, reps_df, ams_df)
                st.rerun()
        with c3:
            if st.button("Clear selected", disabled=(not selected_tid)):
                df = st.session_state["assign_df"].copy()
                mask = df["territory_id"] == str(selected_tid)
                df.loc[mask, ["rep_id", "rep_name", "am_id", "am_name"]] = ""
                st.session_state["assign_df"] = build_assign_df(df, reps_df, ams_df)
                st.rerun()

        st.divider()

        st.caption("Bulk edit (MVP replacement of drag&drop): edit REP/AM IDs in table, then Apply.")

        view_df = st.session_state["assign_df"].copy()
        view_df["territory"] = view_df["territory_name"] + " (" + view_df["weight"].map(lambda x: f"{float(x):.2f}") + ")"
        view_df = view_df[["territory_id", "territory", "rep_id", "rep_name", "am_id", "am_name"]]

        edited = st.data_editor(
            view_df,
            use_container_width=True,
            num_rows="fixed",
            column_config={
                "territory_id": st.column_config.TextColumn("Territory ID", disabled=True),
                "territory": st.column_config.TextColumn("Territory (weight)", disabled=True),
                "rep_id": st.column_config.TextColumn("REP Id"),
                "rep_name": st.column_config.TextColumn("REP Name", disabled=True),
                "am_id": st.column_config.TextColumn("AM Id"),
                "am_name": st.column_config.TextColumn("AM Name", disabled=True),
            },
            key="assign_editor",
        )

        if st.button("Apply table edits"):
            df = st.session_state["assign_df"].copy()
            df["rep_id"] = edited["rep_id"].astype(str).str.strip()
            df["am_id"] = edited["am_id"].astype(str).str.strip()
            st.session_state["assign_df"] = build_assign_df(df, reps_df, ams_df)
            st.success("Edits applied.")
            st.rerun()

        st.divider()

        # KPIs
        kpi_rep = (
            st.session_state["assign_df"]
            .assign(rep_id=lambda d: d["rep_id"].astype(str).str.strip())
            .query("rep_id != ''")
            .groupby(["rep_id", "rep_name"], as_index=False)
            .agg(territories=("territory_id", "count"), weight=("weight", "sum"))
            .sort_values(["weight"], ascending=False)
        )
        kpi_am = (
            st.session_state["assign_df"]
            .assign(am_id=lambda d: d["am_id"].astype(str).str.strip())
            .query("am_id != ''")
            .groupby(["am_id", "am_name"], as_index=False)
            .agg(territories=("territory_id", "count"), weight=("weight", "sum"))
            .sort_values(["weight"], ascending=False)
        )

        unassigned = st.session_state["assign_df"][st.session_state["assign_df"]["rep_id"].astype(str).str.strip() == ""]
        st.warning(f"Unassigned territories to REP: {len(unassigned)} | total weight={unassigned['weight'].sum():.2f}")

        st.write("#### REP KPI")
        st.dataframe(kpi_rep, use_container_width=True, hide_index=True)
        st.write("#### AM KPI")
        st.dataframe(kpi_am, use_container_width=True, hide_index=True)


st.divider()


# -----------------------------
# Export
# -----------------------------
st.subheader("3) Export")

assign_df = st.session_state["assign_df"].copy()
if assign_df.empty:
    st.info("Nothing to export yet.")
else:
    alloc_df = assign_df[["territory_id", "territory_name", "weight", "rep_id", "rep_name", "am_id", "am_name"]].copy()

    sum_rep = (
        alloc_df.assign(rep_id=lambda d: d["rep_id"].astype(str).str.strip())
        .query("rep_id != ''")
        .groupby(["rep_id", "rep_name"], as_index=False)
        .agg(territories=("territory_id", "count"), weight=("weight", "sum"))
        .sort_values("weight", ascending=False)
    )

    sum_am = (
        alloc_df.assign(am_id=lambda d: d["am_id"].astype(str).str.strip())
        .query("am_id != ''")
        .groupby(["am_id", "am_name"], as_index=False)
        .agg(territories=("territory_id", "count"), weight=("weight", "sum"))
        .sort_values("weight", ascending=False)
    )

    c1, c2, c3 = st.columns(3)
    with c1:
        st.write("**Allocations preview**")
        st.dataframe(alloc_df.head(100), use_container_width=True)
    with c2:
        st.write("**REP Summary**")
        st.dataframe(sum_rep, use_container_width=True)
        st.write("**AM Summary**")
        st.dataframe(sum_am, use_container_width=True)
    with c3:
        st.write("**Downloads**")
        st.download_button(
            "Download allocations CSV",
            data=alloc_df.to_csv(index=False).encode("utf-8"),
            file_name="allocations.csv",
            mime="text/csv",
        )
        st.download_button(
            "Download rep_summary CSV",
            data=sum_rep.to_csv(index=False).encode("utf-8"),
            file_name="rep_summary.csv",
            mime="text/csv",
        )
        st.download_button(
            "Download am_summary CSV",
            data=sum_am.to_csv(index=False).encode("utf-8"),
            file_name="am_summary.csv",
            mime="text/csv",
        )

        # XLSX (if openpyxl exists)
        try:
            xlsx_bytes = df_to_xlsx_bytes(
                {"Allocations": alloc_df, "REP_Summary": sum_rep, "AM_Summary": sum_am}
            )
            st.download_button(
                "Download XLSX",
                data=xlsx_bytes,
                file_name="exports.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        except Exception:
            st.warning("XLSX disabled (missing openpyxl). Use CSV.")

st.write("**Map HTML export** (includes admin units colored by REP + REP/AM pins)")
m = make_map(show_admin_layer=True, show_assigned_colors=True)
html_bytes = save_map_html(m)
st.download_button(
    "Download map.html",
    data=html_bytes,
    file_name="map.html",
    mime="text/html",
)
