from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, Optional, List

import pandas as pd
import streamlit as st
import folium
from folium.features import GeoJsonTooltip, GeoJsonPopup
from streamlit_folium import st_folium

from core.normalize import safe_email
from core.io import read_csv_any, validate_fieldforce, validate_weights
from core.geo import extract_admin_index
from core.scoring import compute_total_weight
from core.project import Project, Territory
from core.exports import build_allocations_table, summary_pivot, df_to_xlsx_bytes, save_map_html  # <- se import error: core.export

APP_TITLE = "Territory Allocation MVP (Pharma)"
AUTH_CSV_PATH = Path("data/auth/licensed_users.csv")

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


def ensure_session_defaults():
    st.session_state.setdefault("logged_in", False)
    st.session_state.setdefault("user_email", "")
    st.session_state.setdefault("fieldforce_df", pd.DataFrame())
    st.session_state.setdefault("weights_df", pd.DataFrame())
    st.session_state.setdefault("admin_geojson", None)
    st.session_state.setdefault("admin_by_id", {})
    st.session_state.setdefault("project", Project(project_name="Territory Sizing"))
    st.session_state.setdefault("selected_admin_ids", [])
    st.session_state.setdefault("map_center", [45.94, 24.97])
    st.session_state.setdefault("map_zoom", 6)
    st.session_state.setdefault("last_popup_tid", None)
    st.session_state.setdefault("reps_df", pd.DataFrame())
    st.session_state.setdefault("ams_df", pd.DataFrame())
    st.session_state.setdefault("admin_units_df", pd.DataFrame())  # tab “territori base”
    st.session_state.setdefault("active_rep", "")
    st.session_state.setdefault("active_am", "")


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
    st.download_button(
        "Download fieldforce template",
        data=Path("data/templates/fieldforce_template.csv").read_bytes()
        if Path("data/templates/fieldforce_template.csv").exists()
        else b"",
        file_name="fieldforce_template.csv",
        disabled=not Path("data/templates/fieldforce_template.csv").exists(),
    )
    st.download_button(
        "Download weights template",
        data=Path("data/templates/weights_template.csv").read_bytes()
        if Path("data/templates/weights_template.csv").exists()
        else b"",
        file_name="weights_template.csv",
        disabled=not Path("data/templates/weights_template.csv").exists(),
    )

st.divider()

# -----------------------------
# Sidebar: project config + load/save
# -----------------------------
with st.sidebar:
    st.header("Project")
    st.session_state["project"].project_name = st.text_input(
        "Project name", st.session_state["project"].project_name
    )

    country = st.selectbox("Country", ["Romania"], index=0)
    level = st.selectbox("Territorial level", ["Judete+BucharestSectors"], index=0)
    st.session_state["project"].country = country
    st.session_state["project"].level = level

    st.subheader("Save / Open")
    save_name = st.text_input("Save filename", value="project_romania.json")

    # NB: Streamlit non permette di "creare" un download button al click di un altro button in modo intuitivo.
    # Qui lasciamo il comportamento "Download" fuori, sempre disponibile.
    project_bytes = st.session_state["project"].to_json().encode("utf-8")
    st.download_button(
        "Download project JSON",
        data=project_bytes,
        file_name=save_name,
        mime="application/json",
    )

    uploaded_project = st.file_uploader("Open project JSON", type=["json"])
    if uploaded_project is not None:
        try:
            s = uploaded_project.read().decode("utf-8")
            st.session_state["project"] = Project.from_json(s)
            st.session_state["selected_admin_ids"] = []
            st.session_state["last_popup_tid"] = None
            st.success("Project loaded.")
            st.rerun()
        except Exception as e:
            st.error(f"Could not load project: {e}")

# -----------------------------
# Data loaders
# -----------------------------
st.subheader("1) Load data (CSV + GeoJSON)")
##ATTENZIONE SECONDO ME QUI C'è roba doppia: c'è il vecchio file fieldforce che non serve più
c1, c2, c3 = st.columns(3)
with c1:
    up_weights = st.file_uploader(
        "Weights CSV (territory_id, name, weight)", type=["csv"], key="weights_upl"
    )
with c2:
    up_field = st.file_uploader(
        "Fieldforce CSV (fieldforce.csv)", type=["csv"], key="field_upl"
    )
with c3:
    up_geo = st.file_uploader(
        "Admin GeoJSON (judete + sectors)", type=["geojson", "json"], key="geo_upl"
    )

up_reps = st.file_uploader("REPs CSV (User_Id, Name, Surname, Email)", type=["csv"], key="reps_upl")
up_ams  = st.file_uploader("AMs CSV (User_Id, Name, Surname, Email)", type=["csv"], key="ams_upl")
#Prima validazione
def validate_people_df(df: pd.DataFrame, label: str) -> pd.DataFrame:
    required = ["User_Id", "Name", "Surname", "Email"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        st.error(f"{label}: missing columns {missing}")
        return pd.DataFrame()
    df = df.copy()
    df["User_Id"] = df["User_Id"].astype(str).str.strip()
    df["Email"] = df["Email"].astype(str).str.strip().str.lower()
    # drop empty ids
    df = df[df["User_Id"] != ""]
    # unique check
    if df["User_Id"].duplicated().any():
        dups = df[df["User_Id"].duplicated()]["User_Id"].tolist()
        st.error(f"{label}: duplicated User_Id: {dups[:10]}")
        return pd.DataFrame()
    return df

if up_reps is not None:
    reps = read_csv_any(up_reps)
    reps = validate_people_df(reps, "REPs")
    st.session_state["reps_df"] = reps

if up_ams is not None:
    ams = read_csv_any(up_ams)
    ams = validate_people_df(ams, "AMs")
    st.session_state["ams_df"] = ams

#seconda (forse doppia?  o incorporabile prima?)
reps["Lat"] = pd.to_numeric(reps["Lat"], errors="coerce")
reps["Long"] = pd.to_numeric(reps["Long"], errors="coerce")

issues: List[str] = []

if up_weights is not None:
    wdf = read_csv_any(up_weights)
    wdf, w_issues = validate_weights(wdf)
    st.session_state["weights_df"] = wdf
    if w_issues:
        issues.append(f"Weights issues: {w_issues}")

if up_field is not None:
    fdf = read_csv_any(up_field)
    fdf, f_issues = validate_fieldforce(fdf)
    st.session_state["fieldforce_df"] = fdf
    if f_issues:
        issues.append(f"Fieldforce issues: {f_issues}")

if up_geo is not None:
    admin_geojson = json.loads(up_geo.read().decode("utf-8"))
    st.session_state["admin_geojson"] = admin_geojson
    by_id, _ = extract_admin_index(admin_geojson, id_prop="territory_id", name_prop="name")
    st.session_state["admin_by_id"] = by_id

if issues:
    st.warning(" | ".join(issues))

with st.expander("Loaded data preview"):
    st.write("**Weights**")
    st.dataframe(st.session_state["weights_df"].head(50), use_container_width=True)
    st.write("**Fieldforce**")
    st.dataframe(st.session_state["fieldforce_df"].head(50), use_container_width=True)
    st.write(
        "**GeoJSON loaded**:",
        st.session_state["admin_geojson"] is not None,
        "| Features:",
        len((st.session_state["admin_geojson"] or {}).get("features", []) or []),
    )

st.divider()



# -----------------------------
# Tabella ADMIN UNIT 
# -----------------------------

def build_admin_units_df(admin_by_id: Dict[str, dict], weights_df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for tid, feat in admin_by_id.items():
        props = (feat or {}).get("properties", {}) or {}
        name = str(props.get("name", tid))
        rows.append({"territory_id": str(tid), "name": name})
    df = pd.DataFrame(rows)

    w = weights_df.copy()
    w["territory_id"] = w["territory_id"].astype(str).str.strip()
    w["weight"] = pd.to_numeric(w["weight"], errors="coerce").fillna(0.0)

    df = df.merge(w[["territory_id", "weight"]], on="territory_id", how="left")
    df["weight"] = df["weight"].fillna(0.0)

    # assignment columns
    df["rep_user_id"] = ""
    df["am_user_id"] = ""
    df["bu"] = ""

    return df

# dopo aver caricato geojson + weights:
if (
    st.session_state["admin_by_id"]
    and not st.session_state["weights_df"].empty
    and st.session_state["admin_units_df"].empty
):
    st.session_state["admin_units_df"] = build_admin_units_df(
        st.session_state["admin_by_id"], st.session_state["weights_df"]
    )






# -----------------------------
# Helpers to build map
# -----------------------------
@st.cache_data(show_spinner=False)
def cached_admin_geojson(admin_geojson: dict) -> dict:
    # cache solo per evitare copie inutili
    return admin_geojson


def _build_admin_to_territory_index(project: Project) -> Dict[str, int]:
    """
    Mappa: admin_unit_id -> index del territorio nel project.territories
    Serve per colorare gli admin units già assegnati a territori esistenti.
    """
    mapping: Dict[str, int] = {}
    for idx, terr in enumerate(project.territories):
        for admin_id in terr.admin_unit_ids or []:
            # se overlap, mantieni il primo (o puoi decidere di sovrascrivere)
            mapping.setdefault(str(admin_id), idx)
    return mapping


def make_map() -> folium.Map:
    m = folium.Map(
        location=st.session_state["map_center"],
        zoom_start=st.session_state["map_zoom"],
        control_scale=True,
    )

    if st.session_state["admin_geojson"] is None:
        folium.LayerControl().add_to(m)
        return m

    admin_geojson = cached_admin_geojson(st.session_state["admin_geojson"])
    rep_layer = folium.FeatureGroup(name="REPs", show=True)
    am_layer  = folium.FeatureGroup(name="AMs", show=False)  # di default spento

    selected_now = set(st.session_state.get("selected_admin_ids", []))
    admin_to_terr_idx = _build_admin_to_territory_index(st.session_state["project"])

    # palette semplice: colori diversi per territori già creati
    palette = [
        "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
        "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf"
    ]

    def style_fn(feature):
        props = feature.get("properties", {}) or {}
        tid = str(props.get("territory_id", "")).strip()

        # base
        fill_opacity = 0.15
        weight = 1
        fill_color = "#cccccc"

        # territorio già assegnato
        if tid in admin_to_terr_idx:
            fill_color = palette[admin_to_terr_idx[tid] % len(palette)]
            fill_opacity = 0.22

        # selezione corrente (override)
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

    folium.GeoJson(
        admin_geojson,
        name="Admin units",
        style_function=style_fn,
        tooltip=GeoJsonTooltip(fields=["name", "territory_id"], aliases=["Name", "ID"], sticky=True),
        popup=GeoJsonPopup(fields=["territory_id"], aliases=["ID"], labels=True),
    ).add_to(m)

    # Reps layer (pins)
    fdf = st.session_state["fieldforce_df"]
    if not fdf.empty and "Lat" in fdf.columns and "Long" in fdf.columns:
        for _, r in fdf.iterrows():
            if pd.isna(r.get("Lat")) or pd.isna(r.get("Long")):
                continue
            rep_name = f"{r.get('Name','')} {r.get('Surname','')}".strip()
            uid = str(r.get("User_Id", "")).strip()
            job = str(r.get("JobTitle", "")).strip()
            bl = str(r.get("BusinessLine", "")).strip()
            popup = f"<b>{rep_name}</b><br/>User_Id: {uid}<br/>{job}<br/>{bl}"
            folium.CircleMarker(
                location=[float(r["Lat"]), float(r["Long"])],
                radius=5,
                popup=folium.Popup(popup, max_width=300),
            ).add_to(m)

    folium.LayerControl().add_to(m)
    return m


def _parse_tid_from_popup(popup_html: str) -> Optional[str]:
    """
    Estrae un territorio_id dal popup HTML generato da GeoJsonPopup.
    È volutamente permissivo.
    """
    if not popup_html:
        return None
    # prova prima pattern tipico "RO_XXX"
    m1 = re.search(r"(RO[_\-][A-Za-z0-9_\-]+)", popup_html)
    if m1:
        return m1.group(1)

    # fallback: primo token id-like
    m2 = re.search(r"([A-Za-z0-9_\-]{3,})", popup_html)
    return m2.group(1) if m2 else None


# -----------------------------
# Territory builder
# -----------------------------
st.subheader("2) Create / Edit commercial territories")

left, right = st.columns([1.2, 1])

with left:
    st.write("### Map")

    m = make_map()
    folium_state = st_folium(
        m,
        width=None,
        height=650,
        returned_objects=["last_object_clicked_popup"],
    )

    popup = folium_state.get("last_object_clicked_popup")
    if popup:
        tid = _parse_tid_from_popup(str(popup))
        if tid:
            # evita loop: rerun solo se cambia il tid cliccato
            if st.session_state.get("last_popup_tid") != tid:
                st.session_state["last_popup_tid"] = tid

                sel = st.session_state.get("selected_admin_ids", [])
                if tid in sel:
                    sel.remove(tid)
                else:
                    sel.append(tid)
                st.session_state["selected_admin_ids"] = sel
                st.rerun()

with right:
    st.write("### Territory editor")

    admin_by_id: Dict[str, dict] = st.session_state["admin_by_id"]
    weights_df = st.session_state["weights_df"]

    # Quick actions (FIX 5)
    cA, cB = st.columns(2)
    with cA:
        if st.button("Clear selection"):
            st.session_state["selected_admin_ids"] = []
            st.session_state["last_popup_tid"] = None
            st.rerun()
    with cB:
        st.caption(f"Selected: {len(st.session_state.get('selected_admin_ids', []))}")

    # Admin units list (for selection)
    admin_ids = list(admin_by_id.keys())
    admin_labels = []
    for tid in admin_ids:
        props = (admin_by_id.get(tid, {}) or {}).get("properties", {}) or {}
        name = str(props.get("name", tid))
        admin_labels.append(f"{name} ({tid})")

    label_to_id = {admin_labels[i]: admin_ids[i] for i in range(len(admin_ids))}

    st.caption("MVP note: territory = group of admin units (judete/sectors). No mandatory fields.")
    t_name = st.text_input("Commercial territory name", value="")

    fdf = st.session_state["fieldforce_df"]
    rep_options = []
    am_options = []
    if not fdf.empty and "User_Id" in fdf.columns:
        rep_options = sorted(set(fdf["User_Id"].astype(str).fillna("").tolist()))
        am_options = sorted(set(fdf["User_Id"].astype(str).fillna("").tolist()))

    rep_user = st.selectbox("Rep User_Id (optional)", options=[""] + rep_options)
    am_user = st.selectbox("AM User_Id (optional)", options=[""] + am_options)

    # multiselect (coerente con click-to-toggle: rimane come “lista”/controllo)
    picked = st.multiselect(
        "Select admin units",
        options=admin_labels,
        default=[lbl for lbl, _tid in label_to_id.items() if _tid in st.session_state["selected_admin_ids"]],
    )
    st.session_state["selected_admin_ids"] = [label_to_id[x] for x in picked]

    total_w = compute_total_weight(st.session_state["selected_admin_ids"], weights_df)
    st.metric("Current selected weight", f"{total_w:,.4f}")

    if st.button("Add territory to project"):
        terr = Territory(
            territory_name=t_name,
            rep_user_id=rep_user,
            am_user_id=am_user,
            admin_unit_ids=list(st.session_state["selected_admin_ids"]),
        )
        st.session_state["project"].territories.append(terr)
        st.session_state["project"].touch()
        st.session_state["selected_admin_ids"] = []
        st.session_state["last_popup_tid"] = None
        st.success("Territory added.")
        st.rerun()

    st.divider()

    st.write("### Existing territories")
    if not st.session_state["project"].territories:
        st.info("No territories yet.")
    else:
        for i, terr in enumerate(st.session_state["project"].territories):
            w = compute_total_weight(terr.admin_unit_ids, weights_df)
            with st.expander(
                f"{i+1}. {terr.territory_name or '(no name)'}  |  weight={w:,.4f}  |  admin={len(terr.admin_unit_ids)}"
            ):
                c1, c2 = st.columns([1, 1])
                with c1:
                    new_name = st.text_input("Name", value=terr.territory_name, key=f"nm_{i}")
                    new_rep = st.text_input("Rep User_Id", value=terr.rep_user_id, key=f"rp_{i}")
                    new_am = st.text_input("AM User_Id", value=terr.am_user_id, key=f"am_{i}")
                with c2:
                    default_labels = []
                    for tid in terr.admin_unit_ids:
                        props = (admin_by_id.get(tid, {}) or {}).get("properties", {}) or {}
                        name = str(props.get("name", tid))
                        lbl = f"{name} ({tid})"
                        if lbl in label_to_id:
                            default_labels.append(lbl)

                    new_picked = st.multiselect(
                        "Admin units",
                        options=admin_labels,
                        default=default_labels,
                        key=f"au_{i}",
                    )
                    new_admin_ids = [label_to_id[x] for x in new_picked]

                c3, c4 = st.columns([1, 1])
                with c3:
                    if st.button("Save changes", key=f"sv_{i}"):
                        terr.territory_name = new_name
                        terr.rep_user_id = new_rep
                        terr.am_user_id = new_am
                        terr.admin_unit_ids = new_admin_ids
                        st.session_state["project"].touch()
                        st.success("Saved.")
                        st.rerun()
                with c4:
                    if st.button("Delete", key=f"del_{i}"):
                        st.session_state["project"].territories.pop(i)
                        st.session_state["project"].touch()
                        st.warning("Deleted.")
                        st.rerun()

st.divider()

# -----------------------------
# Export
# -----------------------------
st.subheader("3) Export")

weights_df = st.session_state["weights_df"]
project = st.session_state["project"]

bl = ""
jt = ""
fdf = st.session_state["fieldforce_df"]
if not fdf.empty:
    c1, c2 = st.columns(2)
    with c1:
        bl = st.selectbox(
            "BusinessLine (for allocation output)",
            options=[""] + sorted(set(fdf["BusinessLine"].astype(str).fillna("").tolist())),
        )
    with c2:
        jt = st.selectbox(
            "JobTitle (for allocation output)",
            options=[""] + sorted(set(fdf["JobTitle"].astype(str).fillna("").tolist())),
        )

alloc_df = build_allocations_table(project, weights_df, business_line=bl, job_title=jt)
sum_df = summary_pivot(project, weights_df)

c1, c2, c3 = st.columns(3)
with c1:
    st.write("**Allocations preview**")
    st.dataframe(alloc_df.head(100), use_container_width=True)
with c2:
    st.write("**Summary preview**")
    st.dataframe(sum_df.head(100), use_container_width=True)
with c3:
    st.write("**Downloads**")
    st.download_button(
        "Download allocations CSV",
        data=alloc_df.to_csv(index=False).encode("utf-8"),
        file_name="allocations.csv",
        mime="text/csv",
        disabled=alloc_df.empty,
    )
    st.download_button(
        "Download summary CSV",
        data=sum_df.to_csv(index=False).encode("utf-8"),
        file_name="summary.csv",
        mime="text/csv",
        disabled=sum_df.empty,
    )
    xlsx_bytes = df_to_xlsx_bytes({"Allocations": alloc_df, "Summary": sum_df})
    st.download_button(
        "Download XLSX",
        data=xlsx_bytes,
        file_name="exports.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        disabled=(alloc_df.empty and sum_df.empty),
    )

st.write("**Map HTML export** (includes admin units + rep pins)")
m = make_map()
html_bytes = save_map_html(m)
st.download_button(
    "Download map.html",
    data=html_bytes,
    file_name="map.html",
    mime="text/html",
)
