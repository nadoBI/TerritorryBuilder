import streamlit as st
import pandas as pd
import folium
from streamlit_folium import st_folium

from core import load_geojson, load_csv, prepare_dataset, compute_isf_summary

st.set_page_config(page_title="Territory Tool MVP", layout="wide")

st.title("Territory Tool – MVP v0.1")
st.caption("Upload → scegli LoB → mappa per ISF + tabella saturazione")

with st.sidebar:
    st.header("Dati")
    territories_file = st.file_uploader("territories.geojson", type=["geojson", "json"])
    allocation_file = st.file_uploader("allocation.csv", type=["csv"])
    isf_file = st.file_uploader("isf.csv", type=["csv"])
    market_file = st.file_uploader("market.csv", type=["csv"])

    st.divider()
    st.header("Parametri")
    lob = st.text_input("LoB (es. VitD)", value="VitD")
    target_mode = st.selectbox("Target per ISF", ["1/n (uguale)", "manuale (non implementato)"])
    target_total = st.number_input("Sellout totale target (solo per 1/n)", min_value=0.0, value=0.0, step=1000.0)

if not (territories_file and allocation_file and isf_file and market_file):
    st.info("Carica i 4 file per iniziare (territories.geojson, allocation.csv, isf.csv, market.csv).")
    st.stop()

try:
    territories_gdf = load_geojson(territories_file)
    allocation_df = load_csv(allocation_file)
    isf_df = load_csv(isf_file)
    market_df = load_csv(market_file)
except Exception as e:
    st.error(f"Errore nel caricamento: {e}")
    st.stop()

# LOB disponibili (se esiste la colonna)
if "lob" in market_df.columns:
    lobs = sorted([x for x in market_df["lob"].dropna().unique()])
    if lobs:
        lob = st.sidebar.selectbox("Seleziona LoB", lobs, index=min(lobs.index(lob), len(lobs)-1) if lob in lobs else 0)

try:
    gdf = prepare_dataset(territories_gdf, allocation_df, isf_df, market_df, lob=lob)
except Exception as e:
    st.error(f"Errore nella preparazione dataset: {e}")
    st.stop()

summary = compute_isf_summary(gdf)
n_isf = max((summary["isf_id"].dropna().nunique() if "isf_id" in summary.columns else 0), 1)

if target_mode.startswith("1/n") and target_total > 0:
    target_per_isf = target_total / n_isf
else:
    # se non dato, usiamo il totale reale / n come riferimento
    target_per_isf = gdf["sellout"].sum() / n_isf if n_isf else 0.0

summary["target"] = target_per_isf
summary["saturation"] = summary["sellout_total"] / summary["target"].replace(0, pd.NA)
summary["saturation"] = summary["saturation"].fillna(0.0)

# Layout
col_map, col_tbl = st.columns([2, 1], gap="large")

with col_tbl:
    st.subheader("Saturazione per ISF")
    st.dataframe(
        summary[["isf_name", "sellout_total", "target", "saturation"]]
        .rename(columns={"isf_name": "ISF", "sellout_total": "Sellout", "saturation": "Saturazione"})
        .style.format({"Sellout": "{:,.0f}", "target": "{:,.0f}", "Saturazione": "{:.2f}"}),
        use_container_width=True,
        height=520,
    )

with col_map:
    st.subheader("Mappa territori (colori per ISF)")

    # Centro mappa
    try:
        center = gdf.geometry.unary_union.centroid
        m = folium.Map(location=[center.y, center.x], zoom_start=6, tiles="CartoDB positron")
    except Exception:
        m = folium.Map(location=[41.9, 12.5], zoom_start=5, tiles="CartoDB positron")

    # palette semplice deterministica (senza dipendenze)
    base_colors = [
        "#1f77b4","#ff7f0e","#2ca02c","#d62728","#9467bd",
        "#8c564b","#e377c2","#7f7f7f","#bcbd22","#17becf"
    ]
    isf_names = sorted(gdf["isf_name"].unique().tolist())
    color_map = {name: base_colors[i % len(base_colors)] for i, name in enumerate(isf_names)}

    def style_fn(feature):
        name = feature["properties"].get("isf_name", "UNASSIGNED")
        return {
            "fillColor": color_map.get(name, "#cccccc"),
            "color": "#333333",
            "weight": 1,
            "fillOpacity": 0.55,
        }

    # aggiungi properties utili al geojson renderizzato
    render_gdf = gdf.copy()
    # Folium vuole json serializzabile: convertiamo alcune colonne
    render_gdf["sellout"] = render_gdf["sellout"].astype(float)
    render_gdf["territory_id"] = render_gdf["territory_id"].astype(str)

    folium.GeoJson(
        data=render_gdf.to_json(),
        name="territories",
        style_function=style_fn,
        tooltip=folium.GeoJsonTooltip(
            fields=["territory_id", "isf_name", "sellout"],
            aliases=["Territory", "ISF", "Sellout"],
            localize=True
        ),
    ).add_to(m)

    folium.LayerControl().add_to(m)
    st_folium(m, use_container_width=True, height=560)

st.divider()
st.subheader("Download (dati di lavoro)")
csv_out = gdf.drop(columns="geometry").to_csv(index=False).encode("utf-8")
st.download_button("Scarica dataset unito (CSV)", data=csv_out, file_name="joined_dataset.csv", mime="text/csv")
