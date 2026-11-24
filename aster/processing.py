"""ASTER UMM metadata and spatial processing helpers."""
from __future__ import annotations

import re
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
from pyproj import Transformer
from shapely.geometry import Polygon, box
from shapely.ops import transform, unary_union

TARGET_CRS = "EPSG:3031"
_TO_3031 = Transformer.from_crs("EPSG:4326", TARGET_CRS, always_xy=True)


def get_attribute(metadata: dict, name: str):
    for attr in metadata.get("AdditionalAttributes", []):
        if attr.get("Name") == name:
            values = attr.get("Values", [])
            return values[0] if values else None
    return None


def is_band_off(gains: Any, band: str):
    """True if OFF, False if active, None if not reported."""
    if not isinstance(gains, str):
        return None
    match = re.search(rf"(?:^|,\s*){re.escape(band)}\s+([A-Z]+)(?=,|$)", gains, re.I)
    if match is None:
        return None
    return match.group(1).upper() == "OFF"


def acquisition_datetime(metadata: dict):
    extent = metadata.get("TemporalExtent", {})
    return extent.get("SingleDateTime") or extent.get("RangeDateTime", {}).get("BeginningDateTime")


def granule_identifier(metadata: dict):
    return get_attribute(metadata, "ASTERGRANULEID") or metadata.get("GranuleUR") or "Unknown"


def metadata_footprint(metadata: dict):
    geometry = metadata.get("SpatialExtent", {}).get("HorizontalSpatialDomain", {}).get("Geometry", {})
    polygons = []
    for item in geometry.get("GPolygons", []):
        points = item.get("Boundary", {}).get("Points", [])
        coords = [(p["Longitude"], p["Latitude"]) for p in points if "Longitude" in p and "Latitude" in p]
        if len(coords) >= 3:
            poly = Polygon(coords)
            if not poly.is_empty and poly.is_valid:
                polygons.append(poly)
    return unary_union(polygons) if polygons else None


def reproject_geometry(geometry, transformer=_TO_3031):
    return transform(transformer.transform, geometry)


def load_aoi_from_file(file_or_path, filename: str | None = None):
    """Read a vector AOI and return a dissolved GeoDataFrame in EPSG:4326."""
    gdf = gpd.read_file(file_or_path)
    if gdf.crs is None:
        raise ValueError("The AOI file has no CRS. Please provide a file with a defined coordinate reference system.")
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty].copy()
    if gdf.empty:
        raise ValueError("The AOI file contains no usable geometries.")
    gdf = gdf.to_crs("EPSG:4326")
    geom = unary_union(gdf.geometry)
    return gpd.GeoDataFrame({"name": [filename or "AOI"]}, geometry=[geom], crs="EPSG:4326")


def build_aoi_from_bbox(west: float, south: float, east: float, north: float):
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise ValueError("Invalid bounding box. Check west/east and south/north values.")
    geom = box(west, south, east, north)
    return gpd.GeoDataFrame({"name": ["Bounding box"]}, geometry=[geom], crs="EPSG:4326")


def build_records(granules: list, aoi_gdf: gpd.GeoDataFrame):
    aoi_geom = unary_union(aoi_gdf.to_crs(TARGET_CRS).geometry)
    aoi_area = aoi_geom.area
    records = []
    for granule in granules:
        metadata = granule.get("umm", {})
        gains = get_attribute(metadata, "ASTERGains")
        footprint = metadata_footprint(metadata)
        scene_area = overlap_area = pct_aoi = pct_scene = np.nan
        footprint_wkt = None
        if footprint is not None:
            projected = reproject_geometry(footprint)
            scene_area = projected.area
            overlap_area = projected.intersection(aoi_geom).area
            if aoi_area > 0:
                pct_aoi = 100 * overlap_area / aoi_area
            if scene_area > 0:
                pct_scene = 100 * overlap_area / scene_area
            footprint_wkt = footprint.wkt
        cloud = metadata.get("CloudCover")
        try:
            cloud = float(cloud) if cloud is not None else np.nan
        except (TypeError, ValueError):
            cloud = np.nan
        off_3n, off_3b = is_band_off(gains, "3N"), is_band_off(gains, "3B")
        stereo_valid = (not off_3n and not off_3b) if off_3n is not None and off_3b is not None else pd.NA
        records.append({
            "granule_id": granule_identifier(metadata),
            "granule_ur": metadata.get("GranuleUR"),
            "datetime": acquisition_datetime(metadata),
            "cloud_cover": cloud,
            "vnir_3n_off": off_3n,
            "vnir_3b_off": off_3b,
            "stereo_valid": stereo_valid,
            "stereo_status": "Stereo-valid" if stereo_valid is True else "Stereo-invalid" if stereo_valid is False else "Unknown",
            "scene_area_km2": scene_area / 1e6 if pd.notna(scene_area) else np.nan,
            "overlap_area_km2": overlap_area / 1e6 if pd.notna(overlap_area) else np.nan,
            "pct_aoi_covered": pct_aoi,
            "pct_scene_in_aoi": pct_scene,
            "footprint_wkt": footprint_wkt,
        })
    df = pd.DataFrame(records)
    if df.empty:
        return df
    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce", utc=True)
    df["year_month"] = df["datetime"].dt.strftime("%Y-%m")
    return df.sort_values("pct_aoi_covered", ascending=False, na_position="last").reset_index(drop=True)


def monthly_summary(df: pd.DataFrame, aoi_gdf: gpd.GeoDataFrame):
    """Summarise granule counts and unioned scene coverage by year/month.

    Coverage is calculated from the union of all available scene footprints
    acquired in each month, intersected with the supplied AOI. Stereo-valid
    counts include only granules classified as Stereo-valid.
    """
    if df.empty or "datetime" not in df:
        return pd.DataFrame(
            columns=["year", "month_num", "valid_granules", "pct_aoi_covered"]
        )

    aoi = unary_union(aoi_gdf.to_crs(TARGET_CRS).geometry)
    if aoi.is_empty or aoi.area <= 0:
        raise ValueError("The AOI has no usable area for monthly coverage calculations.")

    dated = df.dropna(subset=["datetime"]).copy()
    if dated.empty:
        return pd.DataFrame(
            columns=["year", "month_num", "valid_granules", "pct_aoi_covered"]
        )

    # Keep the year/month grouping explicit, matching the EDA notebook.
    dated["year"] = dated["datetime"].dt.year
    dated["month_num"] = dated["datetime"].dt.month

    rows = []
    for (year, month_num), group in dated.groupby(
        ["year", "month_num"], sort=True
    ):
        geometries = []
        for wkt in group["footprint_wkt"].dropna():
            from shapely import wkt as shapely_wkt
            geometries.append(
                reproject_geometry(shapely_wkt.loads(wkt))
            )

        if geometries:
            monthly_union = unary_union(geometries)
            covered = monthly_union.intersection(aoi)
            pct_aoi_covered = 100 * covered.area / aoi.area
        else:
            pct_aoi_covered = np.nan

        rows.append({
            "year": int(year),
            "month_num": int(month_num),
            "valid_granules": int(
                (group["stereo_status"] == "Stereo-valid").sum()
            ),
            "pct_aoi_covered": pct_aoi_covered,
        })

    return (
        pd.DataFrame(rows)
        .sort_values(["year", "month_num"])
        .reset_index(drop=True)
    )
