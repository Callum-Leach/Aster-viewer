# ASTER Data Viewer

A Streamlit prototype for searching ASTER L1A V004 granules through NASA Earthdata, inspecting UMM metadata and scene footprints, filtering candidate scenes, and downloading selected granules.

## Features

- Bounding-box AOI input (WGS84) or vector outline (GeoJSON, GeoPackage, zipped shapefile, or local vector path).
- Earthdata search using configurable collection, provider, version and acquisition dates.
- Scene footprint extraction from UMM metadata.
- Antarctic polar stereographic (EPSG:3031) overlap calculations:
  - percentage of AOI covered by each scene;
  - percentage of each scene inside the AOI;
  - overlap area in km².
- ASTER VNIR 3N/3B gain-state parsing and stereo status.
- Cloud-cover, stereo-status and AOI-coverage filters.
- Folium map with AOI and colour-coded scene footprints.
- Monthly summaries and metadata plots.
- Select granules and download via Earthaccess, optionally organised by year/month.
- Export filtered metadata to CSV.

## Install

Use a Python environment compatible with your geospatial stack. For example:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

If installing on the university HPC, use the site's recommended Python/conda environment and make sure GDAL/PROJ dependencies are available for GeoPandas.

## Run

From the `aster-viewer` directory:

```bash
streamlit run app.py
```

The app opens in your browser. Define the AOI in the sidebar, set the date range and click **Search ASTER granules**.

## NASA Earthdata authentication

Searches use Earthaccess. Authentication is initiated only when you click **Authenticate and download**. Configure Earthaccess credentials using the supported Earthaccess method for your environment (for example, a `.netrc` file or interactive login). Do not hard-code credentials in this project.

## AOI input

- Bounding box coordinates must be WGS84 longitude/latitude in decimal degrees.
- Uploaded vector files must have a defined CRS.
- For HPC-resident shapefiles, use the local file path field. The path must be visible to the machine running Streamlit.
- If a glacier outline is supplied, the Earthdata search uses its WGS84 bounding box; coverage statistics use the actual dissolved outline geometry.

## Important implementation notes

- Search and download are separate. Searching does not download data.
- The app retains the original Earthaccess granule objects in Streamlit session state and maps selected rows back to those objects for download.
- Unknown cloud-cover values are retained by the cloud filter; use the catalogue to inspect them.
- Stereo status is `Unknown` if either 3N or 3B gain state is absent/unparseable.
- The download routine submits one selected granule at a time so that it can organise each scene by acquisition year/month and report individual failures. This is robust but may be slower than batching many granules into one Earthaccess call.
- This is a research prototype. Test a small search and one or two downloads before using it for large-scale retrieval.

## Project layout

```text
aster-viewer/
├── app.py
├── requirements.txt
├── README.md
└── aster/
    ├── __init__.py
    ├── earthdata.py
    └── processing.py
```
