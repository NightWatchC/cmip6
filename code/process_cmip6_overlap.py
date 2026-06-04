"""
CMIP6 County-Level Daily Temperature — Overlap-Weighted Extraction.

Computes county-level daily near-surface air temperature (tas) using
polygon-grid-cell area overlap weighting. For each county polygon and
CMIP6 model grid, grid cell polygons are constructed from 1D rectilinear
coordinates, intersected with county boundaries, and overlap areas are
used as weights for the county-level weighted average.

Overlap weights are computed once per model-grid and reused across all
scenarios and time slices belonging to that model.

Usage:
    python code/process_cmip6_overlap.py
    python code/process_cmip6_overlap.py --model FGOALS-g3
    python code/process_cmip6_overlap.py --model GFDL-CM4 --scenario ssp245
    python code/process_cmip6_overlap.py --weights-only
    python code/process_cmip6_overlap.py --skip-weights
    python code/process_cmip6_overlap.py --assemble-only
    python code/process_cmip6_overlap.py --no-cleanup
"""

import sys
import re
import gc
import shutil
import logging
import argparse
import json
import time as time_module
from pathlib import Path
from datetime import date, timedelta
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from scipy.sparse import csr_matrix

from config import (
    PROJECT_DIR,
    DATA_DIR,
    OVERLAP_OUTPUT_DIR,
    OVERLAP_WEIGHT_DIR,
    OVERLAP_FINAL_DIR,
    OVERLAP_LOG_DIR,
    OVERLAP_TEMP_DIR,
    COUNTY_SHAPEFILE,
    VALID_SCENARIOS,
    MODEL_FILTER,
    MODEL_CALENDAR,
)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class FileTask:
    """A single NetCDF file to process."""
    model: str
    scenario: str
    nc_path: str
    start_yyyymmdd: str
    end_yyyymmdd: str
    grid: str


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger("cmip6_overlap")
logger.setLevel(logging.DEBUG)

_console = logging.StreamHandler(sys.stdout)
_console.setLevel(logging.INFO)
_console.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
logger.addHandler(_console)


def _setup_file_log(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(str(log_path), mode="w", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(fh)


# ---------------------------------------------------------------------------
# Module-level: Regex for CMIP6 tas daily filenames
# ---------------------------------------------------------------------------

FILENAME_RE = re.compile(
    r"^tas_day_"
    r"(?P<model>.+)_"
    r"(?P<scenario>ssp\d+)_"
    r"r1i1p1f1_"
    r"(?P<grid>g[rn]\d*)_"
    r"(?P<start>\d{8})-(?P<end>\d{8})"
    r"\.nc$"
)


# ---------------------------------------------------------------------------
# Module-level utility: 1D cell bounds from coordinate centers
# ---------------------------------------------------------------------------

def compute_1d_cell_bounds(coord: np.ndarray) -> np.ndarray:
    """Infer per-cell [lower, upper] bounds from 1D coordinate centers.

    Boundaries are midpoints between adjacent centers. Outer edges are
    extrapolated by half the adjacent spacing.

    Returns (N, 2) float64 array.
    """
    coord = np.asarray(coord, dtype=np.float64)
    midpoints = (coord[:-1] + coord[1:]) / 2.0
    edge_start = coord[0] - (coord[1] - coord[0]) / 2.0
    edge_end = coord[-1] + (coord[-1] - coord[-2]) / 2.0
    edges = np.concatenate(([edge_start], midpoints, [edge_end]))
    lower = np.minimum(edges[:-1], edges[1:])
    upper = np.maximum(edges[:-1], edges[1:])
    return np.column_stack((lower, upper))


# ---------------------------------------------------------------------------
# Module-level utility: coordinate variable discovery
# ---------------------------------------------------------------------------

def _discover_coord_vars(ds) -> tuple[str, str]:
    """Find latitude and longitude variable names in a NetCDF dataset."""
    lat_var = ""
    lon_var = ""
    for vname, v in ds.coords.items():
        axis = getattr(v, "axis", "").upper()
        units = getattr(v, "units", "").lower()
        if axis == "Y" or "degrees_north" in units:
            lat_var = vname
        elif axis == "X" or "degrees_east" in units:
            lon_var = vname

    if not lat_var:
        for candidate in ("lat", "latitude", "nav_lat"):
            if candidate in ds.coords:
                lat_var = candidate
                break

    if not lon_var:
        for candidate in ("lon", "longitude", "nav_lon"):
            if candidate in ds.coords:
                lon_var = candidate
                break

    return lat_var, lon_var


# ---------------------------------------------------------------------------
# Module-level utility: validate 1D rectilinear grid
# ---------------------------------------------------------------------------

def validate_rectilinear_grid(
    ds, lat_var: str, lon_var: str, context: str
) -> tuple[np.ndarray, np.ndarray]:
    """Return validated 1D monotonic lat/lon coordinate arrays.

    Raises ValueError for unsupported grid types (2D, curvilinear,
    non-monotonic).
    """
    lat_values = np.asarray(ds[lat_var].values)
    lon_values = np.asarray(ds[lon_var].values)

    if lat_values.ndim != 1 or lon_values.ndim != 1:
        raise ValueError(
            "Unsupported grid: lat/lon coordinates must be 1D rectilinear. "
            f"{context}"
        )

    grid_lats = lat_values.astype(np.float64)
    grid_lons = lon_values.astype(np.float64)

    if grid_lats.size < 2 or grid_lons.size < 2:
        raise ValueError(
            "Unsupported grid: lat/lon must have at least two centers. "
            f"{context}"
        )

    if not np.isfinite(grid_lats).all() or not np.isfinite(grid_lons).all():
        raise ValueError(
            "Unsupported grid: lat/lon contain non-finite values. {context}"
        )

    for name, arr in (("lat", grid_lats), ("lon", grid_lons)):
        diffs = np.diff(arr)
        if not (np.all(diffs > 0) or np.all(diffs < 0)):
            raise ValueError(
                f"Unsupported grid: {name} coordinate is not strictly "
                f"monotonic. {context}"
            )

    return grid_lats, grid_lons


# ---------------------------------------------------------------------------
# Module-level utility: calendar-aware date generation
# ---------------------------------------------------------------------------

def _parse_yyyymmdd(s: str) -> date:
    return date(int(s[:4]), int(s[4:6]), int(s[6:8]))


def generate_calendar_dates(
    start_yyyymmdd: str,
    end_yyyymmdd: str,
    calendar: str,
) -> list:
    """Generate all dates in the inclusive range for the given CMIP6 calendar.

    Returns list of datetime.date for Gregorian-compatible calendars,
    list of str (YYYY-MM-DD) for 360_day.
    """
    if calendar == "360_day":
        return _generate_dates_360(start_yyyymmdd, end_yyyymmdd)

    start = _parse_yyyymmdd(start_yyyymmdd)
    end = _parse_yyyymmdd(end_yyyymmdd)
    include_leap = calendar == "proleptic_gregorian"

    dates = []
    current = start
    while current <= end:
        if current.month == 2 and current.day == 29:
            if not include_leap:
                current += timedelta(days=1)
                continue
        dates.append(current)
        current += timedelta(days=1)
    return dates


def _generate_dates_360(start_yyyymmdd: str, end_yyyymmdd: str) -> list[str]:
    """Generate 360-day calendar dates as YYYY-MM-DD strings."""
    sy, sm, sd = map(int, [start_yyyymmdd[:4], start_yyyymmdd[4:6], start_yyyymmdd[6:8]])
    ey, em, ed = map(int, [end_yyyymmdd[:4], end_yyyymmdd[4:6], end_yyyymmdd[6:8]])

    dates = []
    y, m, d = sy, sm, sd
    while (y < ey) or (y == ey and m < em) or (y == ey and m == em and d <= ed):
        dates.append(f"{y:04d}-{m:02d}-{d:02d}")
        d += 1
        if d > 30:
            d = 1
            m += 1
            if m > 12:
                m = 1
                y += 1
    return dates


# ===================================================================
# Phase 1: File Discovery
# ===================================================================

def discover_files(data_dir: Path, valid_scenarios: tuple,
                   model_filter: Optional[set] = None) -> list[FileTask]:
    """Walk data directory and discover all processable CMIP6 NetCDF files.

    Skips historical/ directories and empty scenario directories.
    """
    tasks = []
    if not data_dir.exists():
        logger.error("Data directory not found: %s", data_dir)
        return tasks

    for model_dir in sorted(data_dir.iterdir()):
        if not model_dir.is_dir():
            continue
        model = model_dir.name
        if model_filter and model not in model_filter:
            continue

        for scenario_dir in sorted(model_dir.iterdir()):
            if not scenario_dir.is_dir():
                continue
            scenario = scenario_dir.name
            if scenario == "historical":
                continue
            if scenario not in valid_scenarios:
                continue

            nc_files = sorted(scenario_dir.glob("*.nc"))
            if not nc_files:
                logger.info("No .nc files in %s/%s — data may not be published.",
                            model, scenario)
                continue

            for nc_path in nc_files:
                match = FILENAME_RE.match(nc_path.name)
                if not match:
                    logger.warning(
                        "Filename does not match expected pattern, skipping: %s",
                        nc_path.name)
                    continue

                tasks.append(FileTask(
                    model=model,
                    scenario=scenario,
                    nc_path=str(nc_path),
                    start_yyyymmdd=match.group("start"),
                    end_yyyymmdd=match.group("end"),
                    grid=match.group("grid"),
                ))

    tasks.sort(key=lambda t: (t.model, t.scenario, t.start_yyyymmdd))
    return tasks


def log_discovery_summary(tasks: list[FileTask]) -> None:
    """Log a summary of discovered files grouped by model-scenario."""
    from collections import Counter
    counts = Counter()
    grid_map = {}
    for t in tasks:
        counts[(t.model, t.scenario)] += 1
        grid_map.setdefault((t.model, t.scenario), t.grid)

    logger.info("=== File Discovery Summary ===")
    logger.info("Total processable files: %d", len(tasks))
    for (model, scenario), count in sorted(counts.items()):
        cal = MODEL_CALENDAR.get(model, "unknown")
        grid = grid_map[(model, scenario)]
        logger.info("  %s / %s  (%s) — %d files, calendar=%s",
                    model, scenario, grid, count, cal)

    models_found = {t.model for t in tasks}
    missing_pairs = []
    for model in sorted(models_found):
        for s in VALID_SCENARIOS:
            if (model, s) not in counts:
                missing_pairs.append((model, s))
    for model, scenario in sorted(missing_pairs):
        logger.info("  %s / %s — MISSING (0 files)", model, scenario)


# ===================================================================
# Phase 2: Grid Registry
# ===================================================================

def build_grid_registry(tasks: list[FileTask]) -> dict[tuple[str, str], dict]:
    """Catalog each unique (model, grid_label) grid from CMIP6 NetCDF files.

    Opens one representative file per model-grid to extract lat/lon coordinate
    arrays and stores them for later polygon construction.

    Returns dict mapping (model, grid_label) -> {
        "lat_values": ndarray, "lon_values": ndarray,
        "nlat": int, "nlon": int,
        "lat_var": str, "lon_var": str,
        "nc_path": str  # representative file path
    }
    """
    import xarray as xr

    registry = {}
    seen = set()

    for t in tasks:
        key = (t.model, t.grid)
        if key in seen:
            continue
        seen.add(key)

        context = f"model={t.model}, grid={t.grid}, file={t.nc_path}"
        logger.info("Registering grid: %s / %s from %s",
                    t.model, t.grid, Path(t.nc_path).name)

        try:
            ds = xr.open_dataset(t.nc_path)
            lat_var, lon_var = _discover_coord_vars(ds)
            grid_lats, grid_lons = validate_rectilinear_grid(
                ds, lat_var, lon_var, context)
            registry[key] = {
                "lat_values": grid_lats.copy(),
                "lon_values": grid_lons.copy(),
                "nlat": len(grid_lats),
                "nlon": len(grid_lons),
                "lat_var": lat_var,
                "lon_var": lon_var,
                "nc_path": t.nc_path,
            }
            logger.info("  Grid shape: %d lat x %d lon, lat=[%.4f..%.4f], "
                        "lon=[%.4f..%.4f]",
                        len(grid_lats), len(grid_lons),
                        grid_lats[0], grid_lats[-1],
                        grid_lons[0], grid_lons[-1])
            ds.close()
        except Exception:
            logger.exception("Failed to register grid for %s / %s:",
                             t.model, t.grid)

    logger.info("Grid registry: %d unique (model, grid_label) pairs", len(registry))
    for (model, grid), info in sorted(registry.items()):
        logger.info("  %s / %s: %d x %d", model, grid,
                    info["nlat"], info["nlon"])
    return registry


# ===================================================================
# Phase 3: Overlap Weight Computation
# ===================================================================

def _build_grid_cell_polygons(
    lat_values: np.ndarray,
    lon_values: np.ndarray,
) -> "gpd.GeoDataFrame":
    """Build rectangular grid cell polygons from 1D lat/lon center arrays.

    Returns GeoDataFrame with columns: lat_idx, lon_idx, lat_center,
    lon_center, geometry. CRS is EPSG:4326.
    """
    import geopandas as gpd
    from shapely.geometry import box

    lat_bounds = compute_1d_cell_bounds(lat_values)
    lon_bounds = compute_1d_cell_bounds(lon_values)

    nlat, nlon = len(lat_values), len(lon_values)
    records = []
    for i in range(nlat):
        lat_c = lat_values[i]
        lat_min, lat_max = lat_bounds[i]
        for j in range(nlon):
            lon_c = lon_values[j]
            lon_min, lon_max = lon_bounds[j]
            geom = box(lon_min, lat_min, lon_max, lat_max)
            records.append({
                "lat_idx": i, "lon_idx": j,
                "lat_center": lat_c, "lon_center": lon_c,
                "geometry": geom,
            })

    gdf = gpd.GeoDataFrame(records, crs="EPSG:4326")
    return gdf


def _geodesic_polygon_area(geom, geod) -> float:
    """Compute geodesic area of a polygon in square metres using pyproj."""
    from shapely.geometry import Polygon, MultiPolygon

    if geom.is_empty:
        return 0.0

    if isinstance(geom, MultiPolygon):
        return sum(_geodesic_polygon_area(p, geod) for p in geom.geoms)

    if isinstance(geom, Polygon):
        exterior = geom.exterior
        coords = list(exterior.coords)
        if len(coords) < 4:
            return 0.0
        lons = np.array([c[0] for c in coords])
        lats = np.array([c[1] for c in coords])
        poly_area, _ = geod.polygon_area_perimeter(lons, lats)
        area = abs(poly_area)

        for interior in geom.interiors:
            icoords = list(interior.coords)
            if len(icoords) < 4:
                continue
            ilons = np.array([c[0] for c in icoords])
            ilats = np.array([c[1] for c in icoords])
            ihole_area, _ = geod.polygon_area_perimeter(ilons, ilats)
            area -= abs(ihole_area)

        return area

    return 0.0


def compute_one_weight_table(
    model: str,
    grid_label: str,
    registry_entry: dict,
    county_gdf: "gpd.GeoDataFrame",
    weight_dir: Path,
) -> str:
    """Compute overlap weights for one model-grid and save as Parquet.

    Returns the path to the saved weight table.
    """
    import geopandas as gpd
    from pyproj import Geod

    lat_values = registry_entry["lat_values"]
    lon_values = registry_entry["lon_values"]
    nlat = registry_entry["nlat"]
    nlon = registry_entry["nlon"]

    logger.info("Building grid cell polygons for %s / %s (%d x %d)...",
                model, grid_label, nlat, nlon)
    grid_gdf = _build_grid_cell_polygons(lat_values, lon_values)

    n_cells_total = nlat * nlon
    logger.info("  %d grid cell polygons built", n_cells_total)

    # Build spatial index on grid cells for efficient intersection
    grid_sindex = grid_gdf.sindex

    # Initialize geodesic calculator (WGS84 ellipsoid)
    geod = Geod(ellps="WGS84")

    # Pre-compute county areas using positional indices (avoids issues
    # with duplicate county NAMEs in the shapefile).
    logger.info("Computing county geodesic areas...")
    n_counties = len(county_gdf)
    county_areas = np.empty(n_counties, dtype=np.float64)
    county_names = []
    for pos_idx, (_, row) in enumerate(county_gdf.iterrows()):
        county_areas[pos_idx] = _geodesic_polygon_area(row.geometry, geod)
        name_val = row["NAME"]
        if not isinstance(name_val, str):
            name_val = str(name_val) if pd.notna(name_val) else f"county_{pos_idx}"
        county_names.append(name_val)

    # Find intersections and compute overlap weights
    records = []
    covered_indices = np.zeros(n_counties, dtype=bool)

    for pos_idx, (_, crow) in enumerate(county_gdf.iterrows()):
        cname = crow["NAME"]
        cgeom = crow.geometry
        carea = county_areas[pos_idx]

        if pos_idx % 500 == 0:
            logger.info("  Intersecting counties: %d/%d", pos_idx, n_counties)

        if carea == 0:
            logger.warning("  County %s (idx=%d) has zero area, skipping",
                           cname, pos_idx)
            continue

        # Find candidate grid cells via spatial index
        candidates = grid_sindex.query(cgeom, predicate="intersects")
        if len(candidates) == 0:
            logger.warning("  County %s (idx=%d) has no intersecting grid cells",
                           cname, pos_idx)
            continue

        county_has_coverage = False

        for gidx in candidates:
            grow = grid_gdf.iloc[gidx]
            ggeom = grow.geometry

            try:
                intersection = cgeom.intersection(ggeom)
            except Exception:
                logger.warning(
                    "  Intersection error for county=%s (idx=%d), cell=(%d,%d), "
                    "skipping",
                    cname, pos_idx, grow.lat_idx, grow.lon_idx)
                continue

            if intersection.is_empty:
                continue

            overlap_area = _geodesic_polygon_area(intersection, geod)
            if overlap_area <= 0:
                continue

            county_has_coverage = True

            weight = overlap_area / carea

            records.append({
                "model": model,
                "grid_label": grid_label,
                "county_idx": pos_idx,
                "NAME": cname,
                "lat_idx": int(grow.lat_idx),
                "lon_idx": int(grow.lon_idx),
                "grid_lat_center": float(grow.lat_center),
                "grid_lon_center": float(grow.lon_center),
                "overlap_area_m2": overlap_area,
                "county_area_m2": float(carea),
                "overlap_weight": weight,
            })

        if county_has_coverage:
            covered_indices[pos_idx] = True

    if not records:
        logger.error("No overlap records generated for %s / %s", model, grid_label)
        return ""

    weight_df = pd.DataFrame(records)

    # Report weight-sum deviations per county (group by county_idx to handle
    # duplicates correctly).
    weight_sums = weight_df.groupby("county_idx")["overlap_weight"].sum()
    for cidx, wsum in weight_sums.items():
        cname = county_names[int(cidx)]
        if abs(wsum - 1.0) > 0.01 and abs(wsum - 1.0) < 0.05:
            logger.debug("  County %s (idx=%d) weight sum = %.6f (small deviation)",
                         cname, int(cidx), wsum)
        elif abs(wsum - 1.0) >= 0.05:
            logger.info("  County %s (idx=%d) weight sum = %.6f "
                        "(significant deviation — "
                        "may be partially outside model domain)",
                        cname, int(cidx), wsum)

    # Log counties with zero coverage
    missing_indices = np.where(~covered_indices)[0]
    if len(missing_indices) > 0:
        logger.warning("Counties with zero overlap for %s/%s: %d",
                       model, grid_label, len(missing_indices))
        for mi in missing_indices[:10]:
            logger.warning("  - idx=%d: %s", int(mi), county_names[int(mi)])
        if len(missing_indices) > 10:
            logger.warning("  ... and %d more", len(missing_indices) - 10)

    n_covered = int(covered_indices.sum())

    weight_dir.mkdir(parents=True, exist_ok=True)
    out_path = weight_dir / f"{model}_{grid_label}_overlap_weights.parquet"
    weight_df.to_parquet(str(out_path), index=False)
    logger.info("  Weight table saved: %s (%d rows, %d counties with coverage)",
                out_path.name, len(weight_df), n_covered)

    return str(out_path)


def compute_all_weight_tables(
    grid_registry: dict,
    weight_dir: Path,
) -> dict[tuple[str, str], str]:
    """Compute overlap weight tables for all unique model-grid pairs.

    Returns dict mapping (model, grid_label) -> weight_table_path.
    """
    import geopandas as gpd

    # Load county shapefile once
    logger.info("Loading county boundaries from %s", COUNTY_SHAPEFILE)
    county_gdf = gpd.read_file(str(COUNTY_SHAPEFILE))

    # Handle encoding — the shapefile may use GBK for Chinese characters
    if "NAME" not in county_gdf.columns:
        logger.error("Shapefile missing NAME column. Available: %s",
                     list(county_gdf.columns))
        raise KeyError("NAME column not found in shapefile")

    logger.info("  %d counties loaded, CRS: %s", len(county_gdf), county_gdf.crs)
    logger.info("  County NAME examples: %s", county_gdf["NAME"].head(5).tolist())

    # Ensure valid geometries
    invalid_mask = ~county_gdf.geometry.is_valid
    if invalid_mask.any():
        logger.info("  Repairing %d invalid geometries...", invalid_mask.sum())
        county_gdf.loc[invalid_mask, "geometry"] = (
            county_gdf.loc[invalid_mask, "geometry"].make_valid()
        )

    weight_tables = {}
    total = len(grid_registry)
    for idx, ((model, grid_label), entry) in enumerate(
        sorted(grid_registry.items()), 1
    ):
        logger.info("=== Weight computation [%d/%d]: %s / %s ===",
                    idx, total, model, grid_label)
        t0 = time_module.time()
        try:
            path = compute_one_weight_table(
                model, grid_label, entry, county_gdf, weight_dir)
            if path:
                weight_tables[(model, grid_label)] = path
            elapsed = time_module.time() - t0
            logger.info("  Completed in %.1f min", elapsed / 60)
        except Exception:
            logger.exception("Weight computation failed for %s / %s",
                             model, grid_label)
        gc.collect()

    logger.info("=== Weight computation complete: %d/%d tables created ===",
                len(weight_tables), total)
    return weight_tables


# ===================================================================
# Phase 4: Temperature Extraction with Overlap Weights
# ===================================================================

def _build_sparse_weight_matrix(
    weight_df: pd.DataFrame,
    n_counties: int,
    nlat: int,
    nlon: int,
) -> tuple[csr_matrix, list[str]]:
    """Build a sparse CSR weight matrix W of shape (n_counties, nlat * nlon).

    W[i, lat_idx * nlon + lon_idx] = overlap_weight for county at position i.
    County positions are taken from the 'county_idx' column in the weight table.

    Returns (sparse_matrix, county_names_list) where county_names_list[i] is
    the NAME of county at position i.
    """
    n_cells = nlat * nlon

    row_indices = []
    col_indices = []
    data = []

    for _, row in weight_df.iterrows():
        ri = int(row["county_idx"])
        ci = int(row["lat_idx"]) * nlon + int(row["lon_idx"])
        row_indices.append(ri)
        col_indices.append(ci)
        data.append(float(row["overlap_weight"]))

    # Build county names list ordered by position.
    # Guard against NaN NAME values from shapefile encoding issues.
    name_map = {}
    for _, row in weight_df.iterrows():
        idx = int(row["county_idx"])
        if idx not in name_map:
            name_val = row["NAME"]
            if not isinstance(name_val, str):
                name_val = str(name_val) if pd.notna(name_val) else f"county_{idx}"
            name_map[idx] = name_val
    county_names_list = [name_map.get(i, f"county_{i}") for i in range(n_counties)]

    if not row_indices:
        return csr_matrix((n_counties, n_cells), dtype=np.float64), county_names_list

    W = csr_matrix((data, (row_indices, col_indices)),
                   shape=(n_counties, n_cells), dtype=np.float64)
    return W, county_names_list


def process_one_file(
    task: FileTask,
    weight_dir: Path,
    temp_dir: Path,
) -> Optional[str]:
    """Process one NetCDF file using overlap-weighted extraction.

    Returns the path to the temp Parquet, or None on failure.
    """
    import xarray as xr

    nc_name = Path(task.nc_path).name
    logger.info("Processing: %s", nc_name)

    # Load weight table
    weight_path = weight_dir / f"{task.model}_{task.grid}_overlap_weights.parquet"
    if not weight_path.exists():
        logger.error("Weight table not found: %s", weight_path)
        return None
    weight_df = pd.read_parquet(str(weight_path))
    n_counties_total = int(weight_df["county_idx"].max()) + 1

    if n_counties_total == 0:
        logger.error("Weight table has no counties: %s", weight_path)
        return None

    # Open NetCDF and validate grid shape matches weights
    ds = xr.open_dataset(task.nc_path)
    lat_var, lon_var = _discover_coord_vars(ds)
    nlat = ds.sizes.get(lat_var, 0)
    nlon = ds.sizes.get(lon_var, 0)

    if nlat == 0 or nlon == 0:
        ds.close()
        logger.error("Could not determine grid shape from %s", nc_name)
        return None

    # Build sparse weight matrix
    W, county_names = _build_sparse_weight_matrix(weight_df, n_counties_total, nlat, nlon)
    n_counties = len(county_names)

    # Validate grid against weight table
    lat_values = np.asarray(ds[lat_var].values, dtype=np.float64)
    lon_values = np.asarray(ds[lon_var].values, dtype=np.float64)
    if lat_values.ndim != 1 or lon_values.ndim != 1:
        ds.close()
        raise ValueError(f"Non-1D grid coordinates in {nc_name}")
    if len(lat_values) != nlat or len(lon_values) != nlon:
        logger.warning(
            "Grid shape mismatch: weight table expects %d lat x %d lon, "
            "but file has %d x %d. This file may use a different grid variant.",
            nlat, nlon, len(lat_values), len(lon_values))

    # Generate dates
    calendar = MODEL_CALENDAR.get(task.model, "365_day")
    dates = generate_calendar_dates(task.start_yyyymmdd, task.end_yyyymmdd, calendar)
    n_dates = len(dates)
    time_size = ds.sizes.get("time", 0)

    if time_size != n_dates:
        logger.warning(
            "  Time dimension mismatch: NetCDF has %d steps, "
            "filename implies %d dates.", time_size, n_dates)
        if time_size < n_dates:
            dates = dates[:time_size]
            n_dates = time_size
        elif time_size > n_dates:
            n_dates_orig = n_dates
            n_dates = time_size
            # Extend dates if needed by reading from time coordinate
            try:
                dates = generate_calendar_dates(
                    task.start_yyyymmdd, task.end_yyyymmdd, calendar)
            except Exception:
                dates = dates  # keep truncated
            if len(dates) < n_dates:
                logger.warning("  Cannot extend dates, truncating time dimension")
                n_dates = min(len(dates), time_size)
                dates = dates[:n_dates]
            else:
                n_dates = time_size
                dates = dates[:n_dates]

    logger.debug("  Date range: %s - %s (%d dates, calendar=%s)",
                 task.start_yyyymmdd, task.end_yyyymmdd, n_dates, calendar)

    # Determine date column type
    is_360 = calendar == "360_day"
    date_type = pa.string() if is_360 else pa.date32()

    # Process in time chunks
    chunk_size = 365
    all_chunks = []
    total_rows = 0

    for t_start in range(0, n_dates, chunk_size):
        t_end = min(t_start + chunk_size, n_dates)
        chunk_n = t_end - t_start

        # Read tas chunk
        tas_chunk = ds["tas"].isel(time=slice(t_start, t_end)).values.astype(np.float32)
        if tas_chunk.ndim == 2:
            tas_chunk = tas_chunk[np.newaxis, :, :]

        # Compute weighted temperatures for all time steps in chunk
        for t in range(chunk_n):
            tas_2d = tas_chunk[t]  # (nlat, nlon)
            if tas_2d.shape != (nlat, nlon):
                continue
            flat_tas = tas_2d.ravel().astype(np.float64)
            weighted = W.dot(flat_tas)  # (n_counties,) in float64

            chunk_dates = [dates[t_start + t]] * len(county_names)
            all_chunks.append(pa.table({
                "date": pa.array(chunk_dates, type=date_type),
                "NAME": pa.array(county_names, type=pa.string()),
                "tas_mean_k": pa.array(weighted, type=pa.float32()),
            }))
            total_rows += len(county_names)

    ds.close()

    if not all_chunks:
        logger.warning("No data chunks produced for %s", nc_name)
        return None

    table = pa.concat_tables(all_chunks)
    del all_chunks

    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_path = temp_dir / (
        f"{task.model}_{task.scenario}_"
        f"{task.start_yyyymmdd}_{task.end_yyyymmdd}.parquet"
    )
    pq.write_table(table, str(temp_path), compression="snappy")
    logger.info("  Wrote temp Parquet: %s (%d rows)", temp_path.name, total_rows)
    del table
    gc.collect()

    return str(temp_path)


def process_all_files(
    tasks: list[FileTask],
    weight_dir: Path,
    temp_dir: Path,
) -> dict[tuple[str, str], list[str]]:
    """Process all discovered files. Returns mapping (model, scenario) -> temp paths."""
    results = {}
    temp_dir.mkdir(parents=True, exist_ok=True)

    total = len(tasks)
    succeeded = 0
    failed = 0
    failed_files = []

    for idx, task in enumerate(tasks, 1):
        key = (task.model, task.scenario)
        results.setdefault(key, [])

        try:
            nc_name = Path(task.nc_path).name
            logger.info("[%d/%d] %s / %s / %s",
                        idx, total, task.model, task.scenario, nc_name)
            temp_path = process_one_file(task, weight_dir, temp_dir)
            if temp_path:
                results[key].append(temp_path)
                succeeded += 1
            else:
                failed += 1
                failed_files.append(task.nc_path)
        except Exception:
            logger.exception("FAILED: %s", task.nc_path)
            failed += 1
            failed_files.append(task.nc_path)
            gc.collect()
            continue

        if idx % 5 == 0:
            gc.collect()

    logger.info("=== Processing phase complete ===")
    logger.info("Succeeded: %d, Failed: %d, Total: %d", succeeded, failed, total)
    if failed_files:
        logger.warning("Failed files:")
        for f in failed_files:
            logger.warning("  - %s", f)

    return results


# ===================================================================
# Phase 5: Assembly and QC
# ===================================================================

def assemble_one_model_scenario(
    model: str,
    scenario: str,
    temp_paths: list[str],
    output_dir: Path,
) -> str:
    """Concatenate temp Parquet files into final model-scenario output."""
    out_path = output_dir / f"{model}_{scenario}_county_daily_tas_overlap.parquet"
    logger.info("Assembling: %s (%d temp files)", out_path.name, len(temp_paths))

    temp_paths = sorted(temp_paths)
    first_table = pq.read_table(temp_paths[0])
    schema = first_table.schema
    total_rows = 0

    output_dir.mkdir(parents=True, exist_ok=True)
    with pq.ParquetWriter(str(out_path), schema=schema) as writer:
        for tp in temp_paths:
            tbl = pq.read_table(tp)
            writer.write_table(tbl)
            total_rows += len(tbl)
            del tbl

    del first_table
    logger.info("  Assembly complete: %d rows", total_rows)
    return str(out_path)


def run_qc_checks(
    weight_dir: Path,
    final_dir: Path,
    results: dict[tuple[str, str], list[str]],
    log_dir: Path,
) -> None:
    """Run quality control checks on weight tables and final outputs."""
    log_dir.mkdir(parents=True, exist_ok=True)
    qc_report_lines = []
    qc_report_lines.append("=== QC Report ===\n")

    # --- QC 1: Weight table checks ---
    qc_report_lines.append("--- Weight Table QC ---")
    weight_files = sorted(weight_dir.glob("*_overlap_weights.parquet"))
    qc_report_lines.append(f"Weight tables found: {len(weight_files)}")

    weight_issues = []
    for wf in weight_files:
        wdf = pd.read_parquet(str(wf))
        # Group by county_idx to handle duplicate county NAMEs correctly
        weight_sums = wdf.groupby("county_idx")["overlap_weight"].sum()

        n_total = len(weight_sums)
        n_zero = (weight_sums == 0).sum()
        n_partial = ((weight_sums > 0) & (weight_sums < 0.99)).sum()
        n_over_one = (weight_sums > 1.01).sum()
        n_ok = n_total - n_zero - n_partial - n_over_one

        # Build a idx->NAME lookup for reporting
        idx_name = wdf.groupby("county_idx")["NAME"].first().to_dict()

        qc_report_lines.append(
            f"\n  {wf.stem}: {n_total} counties, "
            f"ok={n_ok}, zero={n_zero}, partial={n_partial}, over1={n_over_one}"
        )

        if n_zero > 0:
            zero_idxs = weight_sums[weight_sums == 0].index.tolist()
            zero_names = [idx_name.get(i, f"idx={i}") for i in zero_idxs[:5]]
            qc_report_lines.append(
                f"    Zero-weight counties: {zero_names}"
                + ("..." if len(zero_idxs) > 5 else ""))
            weight_issues.append(f"ZERO_WEIGHT: {wf.stem}: {len(zero_idxs)} counties")

        if n_partial > 0:
            partial = weight_sums[(weight_sums > 0) & (weight_sums < 0.99)]
            top5 = {idx_name.get(k, f"idx={k}"): v for k, v in partial.nsmallest(5).items()}
            qc_report_lines.append(
                f"    Partial-coverage counties (top 5): {top5}"
            )

        if n_over_one > 0:
            over = weight_sums[weight_sums > 1.01]
            top5 = {idx_name.get(k, f"idx={k}"): v for k, v in over.nlargest(5).items()}
            qc_report_lines.append(
                f"    Over-1 counties: {top5}"
            )

    # --- QC 2: Final output checks ---
    qc_report_lines.append("\n--- Final Output QC ---")
    for (model, scenario), paths in sorted(results.items()):
        out_path = final_dir / f"{model}_{scenario}_county_daily_tas_overlap.parquet"
        if not out_path.exists():
            qc_report_lines.append(f"  MISSING: {out_path.name}")
            continue

        tbl = pq.read_table(str(out_path))
        n_rows = len(tbl)
        n_counties = len(tbl.column("NAME").unique().to_pylist())
        temp_col = tbl.column("tas_mean_k").to_numpy()

        temp_min = float(np.nanmin(temp_col))
        temp_max = float(np.nanmax(temp_col))
        temp_mean = float(np.nanmean(temp_col))
        n_nan = int(np.isnan(temp_col).sum())

        qc_report_lines.append(
            f"  {out_path.name}: {n_rows} rows, {n_counties} counties, "
            f"tas=[{temp_min:.1f}, {temp_max:.1f}] K, mean={temp_mean:.1f} K, "
            f"NaN={n_nan}"
        )

        if temp_min < 200 or temp_max > 330:
            qc_report_lines.append(
                f"    WARNING: Temperature outside plausible range (200-330 K)")
        if n_nan > 0:
            qc_report_lines.append(f"    WARNING: {n_nan} NaN values found")

        del tbl

    # Write report
    report_path = log_dir / "qc_report.txt"
    report_path.write_text("\n".join(qc_report_lines), encoding="utf-8")
    logger.info("QC report written to %s", report_path)

    # Write JSON summary for weight issues
    summary_path = log_dir / "qc_weight_issues.json"
    with open(summary_path, "w") as f:
        json.dump({"weight_issues": weight_issues}, f, indent=2, ensure_ascii=False)
    logger.info("QC weight issues written to %s", summary_path)


# ===================================================================
# CLI
# ===================================================================

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="CMIP6 county-level overlap-weighted temperature extraction")
    p.add_argument("--model", type=str, default=None,
                   help="Process only this model (e.g. FGOALS-g3)")
    p.add_argument("--scenario", type=str, default=None,
                   choices=VALID_SCENARIOS,
                   help="Process only this scenario. Requires --model.")
    p.add_argument("--weights-only", action="store_true",
                   help="Only compute and save overlap weight tables; skip extraction.")
    p.add_argument("--skip-weights", action="store_true",
                   help="Skip weight computation; use existing weight tables.")
    p.add_argument("--skip-assembly", action="store_true",
                   help="Skip final assembly; only produce temp Parquet files.")
    p.add_argument("--assemble-only", action="store_true",
                   help="Skip processing; assemble outputs from existing temp files.")
    p.add_argument("--no-cleanup", action="store_true",
                   help="Do not delete temp files after assembly.")
    return p


def main(passed_args: Optional[list] = None) -> int:
    if passed_args is None:
        passed_args = sys.argv[1:]

    parser = build_arg_parser()
    args = parser.parse_args(passed_args)

    _setup_file_log(OVERLAP_LOG_DIR / "process_overlap.log")
    logger.info("CMIP6 County-Level Overlap-Weighted Temperature Extraction")
    logger.info("Project: %s", PROJECT_DIR)

    # --- Phase 1: Discover files ---
    logger.info("=== Phase 1: File Discovery ===")
    tasks = discover_files(DATA_DIR, VALID_SCENARIOS, MODEL_FILTER)

    if args.model:
        tasks = [t for t in tasks if t.model == args.model]
        logger.info("Filtered to model=%s: %d files", args.model, len(tasks))
    if args.scenario:
        if not args.model:
            logger.error("--scenario requires --model")
            return 1
        tasks = [t for t in tasks if t.scenario == args.scenario]
        logger.info("Filtered to scenario=%s: %d files", args.scenario, len(tasks))

    if not tasks:
        logger.warning("No files to process after filtering.")
        return 0

    log_discovery_summary(tasks)

    # --- Phase 2: Build grid registry ---
    logger.info("=== Phase 2: Grid Registry ===")
    grid_registry = build_grid_registry(tasks)
    if not grid_registry:
        logger.error("No valid grids discovered. Aborting.")
        return 1

    # --- Phase 3: Compute overlap weights ---
    weight_tables = {}
    if not args.skip_weights:
        logger.info("=== Phase 3: Overlap Weight Computation ===")
        weight_tables = compute_all_weight_tables(grid_registry, OVERLAP_WEIGHT_DIR)
        if not weight_tables:
            logger.error("No weight tables computed. Aborting.")
            return 1
    else:
        # Verify existing weight tables
        for (model, grid_label) in grid_registry:
            wp = OVERLAP_WEIGHT_DIR / f"{model}_{grid_label}_overlap_weights.parquet"
            if wp.exists():
                weight_tables[(model, grid_label)] = str(wp)
            else:
                logger.warning("Weight table missing: %s", wp)
        logger.info("Using %d existing weight tables.", len(weight_tables))

    if args.weights_only:
        logger.info("--weights-only: skipping temperature extraction.")
        return 0

    # --- Assemble-only mode ---
    if args.assemble_only:
        logger.info("=== Assemble-Only Mode ===")
        results = {}
        for p in sorted(OVERLAP_TEMP_DIR.glob("*.parquet")):
            parts = p.stem.split("_")
            if len(parts) >= 2:
                results.setdefault((parts[0], parts[1]), []).append(str(p))
        if not results:
            logger.error("No temp Parquet files found in %s", OVERLAP_TEMP_DIR)
            return 1

        output_paths = []
        for (model, scenario), temp_paths in sorted(results.items()):
            op = assemble_one_model_scenario(
                model, scenario, temp_paths, OVERLAP_FINAL_DIR)
            output_paths.append(op)

        run_qc_checks(OVERLAP_WEIGHT_DIR, OVERLAP_FINAL_DIR, results, OVERLAP_LOG_DIR)

        logger.info("=== Done ===")
        for p in output_paths:
            fsize = Path(p).stat().st_size / (1024 ** 3)
            logger.info("  %s (%.2f GB)", p, fsize)
        return 0

    # --- Phase 4: Process all files ---
    logger.info("=== Phase 4: Temperature Extraction ===")
    logger.info("Processing %d files across %d model-scenario pairs.",
                len(tasks), len({(t.model, t.scenario) for t in tasks}))

    try:
        results = process_all_files(tasks, OVERLAP_WEIGHT_DIR, OVERLAP_TEMP_DIR)

        output_paths = []
        if not args.skip_assembly:
            logger.info("=== Phase 5: Assembly ===")
            for (model, scenario), temp_paths in sorted(results.items()):
                if not temp_paths:
                    logger.warning("No temp files for %s/%s, skipping.", model, scenario)
                    continue
                op = assemble_one_model_scenario(
                    model, scenario, temp_paths, OVERLAP_FINAL_DIR)
                output_paths.append(op)

            # QC checks
            run_qc_checks(OVERLAP_WEIGHT_DIR, OVERLAP_FINAL_DIR,
                         results, OVERLAP_LOG_DIR)

        # Cleanup temp files (unless --no-cleanup)
        if not args.no_cleanup and not args.skip_assembly:
            if OVERLAP_TEMP_DIR.exists():
                shutil.rmtree(str(OVERLAP_TEMP_DIR), ignore_errors=True)
                logger.info("Temp directory removed.")
        elif args.no_cleanup:
            logger.info("Cleanup skipped (--no-cleanup). Temp files kept.")

        logger.info("=== Done ===")
        logger.info("Output Parquet files: %d", len(output_paths))
        for p in output_paths:
            fsize = Path(p).stat().st_size / (1024 ** 3)
            logger.info("  %s (%.2f GB)", p, fsize)

    except KeyboardInterrupt:
        logger.warning("Interrupted by user.")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
