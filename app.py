from __future__ import annotations

from pathlib import Path
import tempfile
import zipfile

import folium
import geopandas as gpd
import pandas as pd
import plotly.express as px
import streamlit as st
from streamlit_folium import st_folium

from aster.earthdata import authenticate, download_granules, search_aster
from aster.processing import build_aoi_from_bbox, build_records, load_aoi_from_file, monthly_summary

st.set_page_config(page_title="ASTER Data Viewer", page_icon="🛰️", layout="wide")
st.title("🛰️ ASTER Data Viewer")
st.caption("Discover ASTER L1A granules, inspect their metadata and spatial coverage, then download only the scenes you select.")

# ----------------------------- Session state -----------------------------
for key, default in {
    "granules": None,
    "results_df": None,
    "aoi_gdf": None,
    "results_aoi_gdf": None,
    "aoi_label": None,
    "search_bbox": None,
    "download_log": [],
    "selected_granule_ids": [],
    "results_aoi_is_vector": False,
}.items():
    if key not in st.session_state:
        st.session_state[key] = default


def select_all_granules(granule_ids):
    """Select every granule returned by the current search."""
    st.session_state.selected_granule_ids = list(granule_ids)


def clear_granule_selection():
    st.session_state.selected_granule_ids = []


def toggle_visible_granules():
    """Select or deselect all granules currently shown in the catalogue."""
    selected = set(st.session_state.get("selected_granule_ids", []))
    visible = set(st.session_state.get("visible_granule_ids", []))

    if st.session_state.get("select_all_visible", False):
        selected.update(visible)
    else:
        selected.difference_update(visible)

    all_ids = st.session_state.get("all_granule_ids", [])
    st.session_state.selected_granule_ids = [
        granule_id for granule_id in all_ids if granule_id in selected
    ]


# ----------------------------- Sidebar: AOI -----------------------------
st.sidebar.header("1 · Area of interest")
aoi_mode = st.sidebar.radio("Define AOI using", ["Bounding box", "Vector outline"], horizontal=False)
aoi_gdf = None
bbox = None
aoi_is_vector = False

if aoi_mode == "Bounding box":
    st.sidebar.caption("Coordinates are WGS84 (EPSG:4326), in decimal degrees.")
    c1, c2 = st.sidebar.columns(2)
    west = c1.number_input("West / min lon", value=-67.5799499148, format="%.6f")
    south = c2.number_input("South / min lat", value=-69.9072878847, format="%.6f")
    c3, c4 = st.sidebar.columns(2)
    east = c3.number_input("East / max lon", value=-64.1051930129, format="%.6f")
    north = c4.number_input("North / max lat", value=-69.2613136001, format="%.6f")
    try:
        aoi_gdf = build_aoi_from_bbox(west, south, east, north)
        bbox = (west, south, east, north)
    except ValueError as exc:
        st.sidebar.error(str(exc))
else:
    st.sidebar.caption(
        "Upload a zipped shapefile, or select all the shapefile components "
        "(.shp, .shx, .dbf and .prj) together. You can also provide a path "
        "to a shapefile on the machine running Streamlit."
    )
    uploaded_files = st.sidebar.file_uploader(
        "Upload glacier outline / AOI",
        type=["shp", "shx", "dbf", "prj", "cpg", "zip", "geojson", "json", "gpkg"],
        accept_multiple_files=True,
    )
    vector_path = st.sidebar.text_input(
        "Or vector file path",
        placeholder="/path/to/glacier_outline.shp",
    )

    try:
        if uploaded_files:
            with tempfile.TemporaryDirectory(prefix="aster_aoi_") as temp_dir:
                temp_dir = Path(temp_dir)

                # Save all uploaded components together so GeoPandas/GDAL can
                # read a complete ESRI Shapefile (rather than an isolated .shp).
                zip_uploads = [f for f in uploaded_files if Path(f.name).suffix.lower() == ".zip"]
                shp_uploads = [f for f in uploaded_files if Path(f.name).suffix.lower() == ".shp"]

                if zip_uploads:
                    if len(uploaded_files) != 1:
                        raise ValueError("Upload either one ZIP archive or the individual shapefile components, not both.")
                    archive_path = temp_dir / Path(zip_uploads[0].name).name
                    archive_path.write_bytes(zip_uploads[0].getvalue())
                    with zipfile.ZipFile(archive_path) as archive:
                        archive.extractall(temp_dir / "extracted")
                    shp_paths = list((temp_dir / "extracted").rglob("*.shp"))
                    if len(shp_paths) != 1:
                        raise ValueError("The ZIP must contain exactly one .shp file (and its supporting files).")
                    shp_path = shp_paths[0]
                    aoi_gdf = load_aoi_from_file(shp_path, shp_path.stem)

                elif shp_uploads:
                    for uploaded_file in uploaded_files:
                        destination = temp_dir / Path(uploaded_file.name).name
                        destination.write_bytes(uploaded_file.getvalue())

                    shp_path = temp_dir / Path(shp_uploads[0].name).name
                    required_sidecars = [".shx", ".dbf", ".prj"]
                    missing = [
                        ext for ext in required_sidecars
                        if not shp_path.with_suffix(ext).exists()
                    ]
                    if missing:
                        raise ValueError(
                            "The shapefile is missing required companion file(s): "
                            + ", ".join(missing)
                            + ". Upload the complete shapefile set or a ZIP containing it."
                        )
                    aoi_gdf = load_aoi_from_file(shp_path, shp_path.stem)

                else:
                    if len(uploaded_files) != 1:
                        raise ValueError("Upload one GeoJSON or GeoPackage file, or a complete shapefile set.")
                    uploaded_file = uploaded_files[0]
                    destination = temp_dir / Path(uploaded_file.name).name
                    destination.write_bytes(uploaded_file.getvalue())
                    aoi_gdf = load_aoi_from_file(destination, destination.name)

            aoi_is_vector = True

        elif vector_path.strip():
            aoi_gdf = load_aoi_from_file(vector_path.strip(), Path(vector_path).name)
            aoi_is_vector = True

        if aoi_gdf is not None:
            minx, miny, maxx, maxy = aoi_gdf.total_bounds
            bbox = (minx, miny, maxx, maxy)

    except Exception as exc:
        st.sidebar.error(f"Could not read AOI: {exc}")

st.sidebar.divider()
st.sidebar.header("2 · Search settings")
short_name = st.sidebar.text_input("Collection short name", value="AST_L1A")
provider = st.sidebar.text_input("Provider", value="LPCLOUD")
version = st.sidebar.text_input("Collection version", value="004")
start_date = st.sidebar.date_input("Start date", value=pd.Timestamp("2022-01-01").date(), min_value=pd.Timestamp("1900-01-01").date(), max_value=pd.Timestamp("2100-01-01").date())
end_date = st.sidebar.date_input("End date", value=pd.Timestamp("2022-12-31").date(), min_value=pd.Timestamp("1900-01-01").date(), max_value=pd.Timestamp("2100-01-01").date())
search_clicked = st.sidebar.button("🔎 Search ASTER granules", type="primary", use_container_width=True)

if search_clicked:
    if aoi_gdf is None or bbox is None:
        st.sidebar.error("Define a valid AOI before searching.")
    elif start_date > end_date:
        st.sidebar.error("Start date must be on or before end date.")
    else:
        try:
            with st.spinner("Searching NASA Earthdata and processing granule metadata…"):
                granules = search_aster(bbox, start_date.isoformat(), end_date.isoformat(), short_name, provider, version)
                df = build_records(granules, aoi_gdf)
                st.session_state.granules = granules
                st.session_state.results_df = df
                st.session_state.aoi_gdf = aoi_gdf
                st.session_state.results_aoi_gdf = aoi_gdf.copy()
                st.session_state.aoi_label = aoi_gdf.iloc[0].get("name", "AOI")
                st.session_state.search_bbox = bbox
                st.session_state.results_aoi_is_vector = aoi_is_vector
            st.sidebar.success(f"Found {len(granules)} granules.")
        except Exception as exc:
            st.sidebar.exception(exc)

# Preserve current AOI for mapping even before/after searches.
if aoi_gdf is not None:
    st.session_state.aoi_gdf = aoi_gdf

# ----------------------------- Main interface ----------------------------
if st.session_state.results_df is None:
    st.info("Define an area of interest in the sidebar, choose an acquisition date range, and search Earthdata to begin.")
    st.markdown("**Defaults** are set to the Fleming Glacier bounding box and ASTER L1A V004 at LPCLOUD, matching the supplied exploration notebook.")
    st.stop()

df_all = st.session_state.results_df.copy()
granules_all = st.session_state.granules or []
aoi_for_results = st.session_state.results_aoi_gdf

# Map original Earthaccess granule objects back to dataframe rows by GranuleUR.
granule_by_ur = {}
for granule in granules_all:
    umm = granule.get("umm", {})
    ur = umm.get("GranuleUR")
    if ur:
        granule_by_ur[ur] = granule

summary_tab, map_tab, table_tab, download_tab = st.tabs(["Overview & statistics", "Map", "Granule catalogue", "Downloads"])

# ----------------------------- Filters shared ----------------------------
with st.sidebar:
    st.divider()
    st.header("3 · Explore filters")
    max_cloud = st.slider("Maximum cloud cover (%)", 0, 100, 100)
    stereo_choice = st.selectbox("Stereo status", ["All", "Stereo-valid", "Stereo-invalid", "Unknown"])
    min_coverage = st.slider("Minimum AOI coverage per scene (%)", 0, 100, 0)

filtered = df_all.copy()
filtered = filtered[filtered["cloud_cover"].isna() | (filtered["cloud_cover"] <= max_cloud)]
if stereo_choice != "All":
    filtered = filtered[filtered["stereo_status"] == stereo_choice]
filtered = filtered[filtered["pct_aoi_covered"].isna() | (filtered["pct_aoi_covered"] >= min_coverage)]

# ----------------------------- Overview -----------------------------------
with summary_tab:
    st.subheader("Search summary")
    total = len(df_all)
    valid = int((df_all["stereo_status"] == "Stereo-valid").sum()) if total else 0
    unknown = int((df_all["stereo_status"] == "Unknown").sum()) if total else 0
    dated = df_all.dropna(subset=["datetime"])
    mean_cloud = df_all["cloud_cover"].mean()
    mean_coverage = df_all["pct_aoi_covered"].mean()
    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("Granules found", f"{total:,}")
    k2.metric("Stereo-valid", f"{valid:,}")
    k3.metric("Stereo unknown", f"{unknown:,}")
    k4.metric("Mean cloud cover", f"{mean_cloud:.1f}%" if pd.notna(mean_cloud) else "—")
    k5.metric("Mean scene AOI coverage", f"{mean_coverage:.1f}%" if pd.notna(mean_coverage) else "—")
    st.caption(f"Showing {len(filtered):,} of {total:,} granules after sidebar filters. Unknown cloud-cover values are retained by the filter and labelled as unknown in the table.")

    # Monthly summaries follow the year/month structure used in the EDA notebook.
    dated = df_all.dropna(subset=["datetime"]).copy()

    if not dated.empty:
        dated["year"] = dated["datetime"].dt.year
        dated["month_num"] = dated["datetime"].dt.month

        # ---------------------------------------------------------
        # 1. Monthly count of stereo-valid granules
        # ---------------------------------------------------------
        monthly_counts = (
            dated.groupby(["year", "month_num"], sort=True)
            .agg(
                valid_granules=(
                    "stereo_status",
                    lambda values: (values == "Stereo-valid").sum(),
                )
            )
            .reset_index()
        )

        months = list(range(1, 13))
        years = sorted(monthly_counts["year"].unique())

        heatmap_df = (
            monthly_counts.pivot(
                index="month_num",
                columns="year",
                values="valid_granules",
            )
            .reindex(index=months, columns=years)
        )

        fig = px.imshow(
            heatmap_df,
            x=heatmap_df.columns,
            y=heatmap_df.index,
            aspect="auto",
            labels={
                "x": "Acquisition year",
                "y": "Acquisition month",
                "color": "Valid granules",
            },
            title="Monthly Counts of Stereo-Valid ASTER Granules",
        )

        fig.update_yaxes(
            tickmode="array",
            tickvals=months,
            ticktext=[
                "Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
            ],
            autorange="reversed",
        )

        fig.update_xaxes(dtick=1)
        fig.update_layout(coloraxis_colorbar={"title": "Valid granules"})
        st.plotly_chart(fig, use_container_width=True)

        # ---------------------------------------------------------
        # 2. Monthly glacier AOI coverage
        # ---------------------------------------------------------
        if aoi_for_results is not None:
            monthly_coverage = monthly_summary(df_all, aoi_for_results)

            coverage_heatmap = (
                monthly_coverage.pivot(
                    index="month_num",
                    columns="year",
                    values="pct_aoi_covered",
                )
                .reindex(index=months, columns=years)
            )

            fig = px.imshow(
                coverage_heatmap,
                x=coverage_heatmap.columns,
                y=coverage_heatmap.index,
                aspect="auto",
                color_continuous_scale="plasma",
                range_color=(0, 100),
                labels={
                    "x": "Acquisition year",
                    "y": "Acquisition month",
                    "color": "AOI covered (%)",
                },
                title="Monthly ASTER Coverage of Area of Interest",
            )

            fig.update_yaxes(
                tickmode="array",
                tickvals=months,
                ticktext=[
                    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
                    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
                ],
                autorange="reversed",
            )

            fig.update_xaxes(dtick=1)
            fig.update_layout(coloraxis_colorbar={"title": "AOI covered (%)"})
            st.plotly_chart(fig, use_container_width=True)

        else:
            st.info("Define an area of interest to calculate monthly coverage.")

    else:
        st.info("No granules with valid acquisition dates were found.")

    st.subheader("Metadata distributions")
    left, right = st.columns(2)
    with left:
        cloud_df = df_all.dropna(subset=["cloud_cover"])
        if not cloud_df.empty:
            st.plotly_chart(px.histogram(cloud_df, x="cloud_cover", nbins=20, title="Cloud-cover distribution", labels={"cloud_cover": "Cloud cover (%)"}), use_container_width=True)
    with right:
        st.plotly_chart(px.histogram(df_all, x="stereo_status", color="stereo_status", title="VNIR stereo status", labels={"stereo_status": "Stereo status"}), use_container_width=True)

# ----------------------------- Map ----------------------------------------
with map_tab:
    st.subheader("ASTER scene footprints")
    if aoi_for_results is None:
        st.warning("AOI is not available. Run a new search after defining it.")
    else:
        bounds = aoi_for_results.total_bounds
        minx, miny, maxx, maxy = bounds
        centre = [(miny + maxy) / 2, (minx + maxx) / 2]

        # Expand the AOI extent before fitting the map. This keeps the AOI
        # centred while showing a useful surrounding region by default.
        lon_span = maxx - minx
        lat_span = maxy - miny
        lon_padding = max(lon_span * 0.25, 0.15)
        lat_padding = max(lat_span * 0.25, 0.10)
        view_bounds = [
            [max(-90, miny - lat_padding), max(-180, minx - lon_padding)],
            [min(90, maxy + lat_padding), min(180, maxx + lon_padding)],
        ]
        m = folium.Map(location=centre, zoom_start=6, tiles=None, control_scale=True)
        folium.map.CustomPane("aoiPane", z_index=625).add_to(m)
        folium.TileLayer("OpenStreetMap", name="OpenStreetMap").add_to(m)
        folium.TileLayer(tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", attr="Esri", name="Esri satellite", overlay=False, control=True).add_to(m)
        folium.GeoJson(aoi_for_results.to_json(), name="Area of interest", pane="aoiPane", style_function=lambda _: {"color": "red", "weight": 3, "fillColor": "red", "fillOpacity": 0.04}).add_to(m)
        map_source = filtered if st.checkbox("Map only filtered scenes", value=True) else df_all
        colours = {"Stereo-valid": "#16803c", "Stereo-invalid": "#d97706", "Unknown": "#64748b"}
        for _, row in map_source.iterrows():
            wkt = row.get("footprint_wkt")
            if not isinstance(wkt, str) or not wkt:
                continue
            try:
                from shapely import wkt as shapely_wkt
                geom = shapely_wkt.loads(wkt)
                feature = gpd.GeoSeries([geom], crs="EPSG:4326")
                popup = f"<b>{row['granule_id']}</b><br>Date: {row['datetime']}<br>Cloud: {row['cloud_cover']}%<br>Stereo: {row['stereo_status']}<br>AOI coverage: {row['pct_aoi_covered']:.1f}%" if pd.notna(row["pct_aoi_covered"]) else f"<b>{row['granule_id']}</b>"
                folium.GeoJson(feature.to_json(), style_function=lambda _, c=colours.get(row["stereo_status"], "#2563eb"): {"color": c, "weight": 1.5, "fillColor": c, "fillOpacity": 0.15}, tooltip=row["granule_id"], popup=folium.Popup(popup, max_width=350)).add_to(m)
            except Exception:
                continue
        folium.LayerControl().add_to(m)
        # Padding is applied in addition to the expanded geographic bounds.
        # max_zoom prevents small AOIs from opening at an excessively close scale.
        m.fit_bounds(view_bounds, padding=(35, 35), max_zoom=7)
        st_folium(m, use_container_width=True, height=650, returned_objects=[])
        st.caption("Green = both VNIR 3N and 3B reported active; orange = at least one reported OFF; grey = stereo status unknown.")

# ----------------------------- Catalogue ----------------------------------
with table_tab:
    st.subheader("Granule catalogue")
    st.write(
        "Tick individual rows to select granules. Use the checkbox above "
        "the table to select or deselect all rows currently shown."
    )

    all_granule_ids = df_all["granule_id"].dropna().astype(str).tolist()
    visible_granule_ids = filtered["granule_id"].dropna().astype(str).tolist()

    # Keep these lists available to the master-checkbox callback.
    st.session_state.all_granule_ids = all_granule_ids
    st.session_state.visible_granule_ids = visible_granule_ids

    selected_set = set(st.session_state.selected_granule_ids)
    all_visible_selected = (
        bool(visible_granule_ids)
        and set(visible_granule_ids).issubset(selected_set)
    )

    # Synchronise the master checkbox with the individual row selections.
    st.session_state.select_all_visible = all_visible_selected
    st.checkbox(
        f"Select all rows shown ({len(visible_granule_ids):,})",
        key="select_all_visible",
        on_change=toggle_visible_granules,
        disabled=not visible_granule_ids,
    )

    # Put the selection checkbox first, followed by the useful metadata.
    display_cols = [
        "granule_id",
        "datetime",
        "cloud_cover",
        "stereo_status",
        "pct_aoi_covered",
        "pct_scene_in_aoi",
        "overlap_area_km2",
    ]

    display = filtered[display_cols].copy().rename(
        columns={
            "granule_id": "Granule ID",
            "datetime": "Acquisition date",
            "cloud_cover": "Cloud cover (%)",
            "stereo_status": "Stereo status",
            "pct_aoi_covered": "AOI covered (%)",
            "pct_scene_in_aoi": "Scene within AOI (%)",
            "overlap_area_km2": "Overlap (km²)",
        }
    )

    display.insert(
        0,
        "Select",
        display["Granule ID"].astype(str).isin(selected_set),
    )

    editor_key = f"granule_catalogue_editor_{abs(hash(tuple(visible_granule_ids)))}"

    edited = st.data_editor(
        display,
        key=editor_key,
        use_container_width=True,
        hide_index=True,
        num_rows="fixed",
        disabled=[column for column in display.columns if column != "Select"],
        column_config={
            "Select": st.column_config.CheckboxColumn(
                "Select",
                help="Tick to include this granule in the download selection.",
                default=False,
            ),
            "Granule ID": st.column_config.TextColumn("Granule ID", width="large"),
        },
    )

    # Preserve selections outside the current filter, while updating the
    # selections for rows currently visible in the table.
    visible_selected = set(
        edited.loc[edited["Select"], "Granule ID"].astype(str).tolist()
    )
    updated_selected = (selected_set - set(visible_granule_ids)) | visible_selected
    ordered_selection = [
        granule_id for granule_id in all_granule_ids
        if granule_id in updated_selected
    ]

    if ordered_selection != st.session_state.selected_granule_ids:
        st.session_state.selected_granule_ids = ordered_selection
        st.rerun()

    selected_count = len(st.session_state.selected_granule_ids)
    st.caption(
        f"{selected_count:,} granules selected out of {len(all_granule_ids):,}. "
        "The master checkbox selects all rows currently visible after filtering."
    )

    csv = filtered.drop(columns=["footprint_wkt"], errors="ignore").to_csv(index=False).encode("utf-8")
    st.download_button("Export filtered metadata (CSV)", data=csv, file_name="aster_granule_metadata.csv", mime="text/csv")

# ----------------------------- Downloads ----------------------------------
with download_tab:
    st.subheader("Download selected granules")
    selected_ids = st.session_state.get("selected_granule_ids", [])
    selected_rows = df_all[df_all["granule_id"].isin(selected_ids)] if selected_ids else pd.DataFrame()
    st.write(f"**{len(selected_rows)} granules** currently selected.")
    if selected_rows.empty:
        st.info("Select granules in the Granule catalogue tab first.")
    else:
        st.dataframe(selected_rows[["granule_id", "datetime", "cloud_cover", "stereo_status", "pct_aoi_covered"]], use_container_width=True, hide_index=True)
        default_root = str(Path.home() / "ASTER-V4" / "downloads")
        destination_root = st.text_input("Download destination", value=default_root, help="The directory is created if it does not exist. Each granule is placed in a YYYY/MM subdirectory.")
        threads = st.number_input("Download threads", min_value=1, max_value=16, value=4, step=1)
        organise_monthly = st.checkbox("Organise into year/month folders", value=True)
        confirm = st.checkbox("I have checked the selected granules and destination.")
        if st.button("Authenticate and download", type="primary", disabled=not confirm, use_container_width=True):
            try:
                with st.spinner("Authenticating with NASA Earthdata…"):
                    authenticate()
                progress = st.progress(0, text="Preparing downloads…")
                total = len(selected_rows)
                completed = 0
                errors = []
                root = Path(destination_root).expanduser()
                root.mkdir(parents=True, exist_ok=True)
                for _, row in selected_rows.iterrows():
                    granule = granule_by_ur.get(row["granule_ur"])
                    if granule is None:
                        errors.append(f"Could not match granule object for {row['granule_id']}")
                        continue
                    dt = row["datetime"]
                    target = root / f"{dt.year:04d}" / f"{dt.month:02d}" if organise_monthly and pd.notna(dt) else root
                    target.mkdir(parents=True, exist_ok=True)
                    try:
                        download_granules([granule], target, threads=threads)
                    except Exception as exc:
                        errors.append(f"{row['granule_id']}: {exc}")
                    completed += 1
                    progress.progress(min(completed / total, 1.0), text=f"Processed {completed}/{total} selected granules")
                st.success(f"Download process finished. Output: {root.resolve()}")
                if errors:
                    st.warning("Some downloads could not be completed:\n\n" + "\n".join(errors))
                else:
                    st.success("All selected granules were submitted to Earthaccess without reported errors.")
            except Exception as exc:
                st.exception(exc)

st.sidebar.caption("ASTER Data Viewer · Prototype. Searches use NASA Earthdata metadata; downloading requires Earthdata credentials.")