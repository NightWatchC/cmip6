"""
CMIP6 County-Level Daily Mean Temperature Processing.

Processes gridded CMIP6 daily near-surface air temperature (tas) NetCDF files
into county-level daily Parquet tables. County centroids come from
data/CountyCoordinate.dta. All processing is pure Python — no ArcPy required.

Only future projection scenarios (ssp126, ssp245, ssp585) are processed.
Historical data is skipped.

Usage:
    python code/process_cmip6.py
    python code/process_cmip6.py --model FGOALS-g3
    python code/process_cmip6.py --model GFDL-CM4 --scenario ssp245
    python code/process_cmip6.py --skip-assembly --no-cleanup
    python code/process_cmip6.py --assemble-only
"""

import time
import os
import sys
import re
import gc
import shutil
import logging
import argparse
from pathlib import Path
from datetime import date, timedelta
from dataclasses import dataclass, field
from typing import List, Optional, Union

import numpy as np
import pandas as pd

from config import (
    PROJECT_DIR,
    DATA_DIR,
    OUTPUT_DIR,
    TEMP_DIR,
    TEMP_PARQUET_DIR,
    COUNTY_COORDINATE_FILE,
    VALID_SCENARIOS,
    MODEL_FILTER,
    MODEL_CALENDAR,
)

TEMP_AUDIT_DIR = TEMP_DIR / "grid_audit_temps"
AUDIT_OUTPUT_DIR = OUTPUT_DIR / "grid_audit"

# ---------------------------------------------------------------------------
# Time Sleep for 2 hours
# ---------------------------------------------------------------------------
def sleep_for_hours(hours: int) -> None:
    """Sleep for the specified number of hours."""
    seconds = hours * 3600
    logger.info("Sleeping for %d hours (%d seconds)...", hours, seconds)
    time.sleep(seconds)

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


@dataclass
class ProcessingSummary:
    """Aggregated results from a processing run."""
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    missing_scenarios: list[str] = field(default_factory=list)
    failed_files: list[str] = field(default_factory=list)
    output_paths: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger("cmip6")
logger.setLevel(logging.DEBUG)

_console = logging.StreamHandler(sys.stdout)
_console.setLevel(logging.INFO)
_console.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
logger.addHandler(_console)


def _setup_file_log(log_path: Path) -> None:
    """Add file handler for DEBUG-level logging."""
    _file = logging.FileHandler(str(log_path), mode="w", encoding="utf-8")
    _file.setLevel(logging.DEBUG)
    _file.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(_file)


# ---------------------------------------------------------------------------
# Load county centroids from .dta (one-time, module init)
# ---------------------------------------------------------------------------

_centroid_df = pd.read_stata(str(COUNTY_COORDINATE_FILE))
_CENTROID_NAMES: list[str] = _centroid_df["county"].tolist()
_CENTROID_LONS: np.ndarray = _centroid_df["centlon"].values.astype(np.float64)
_CENTROID_LATS: np.ndarray = _centroid_df["centlat"].values.astype(np.float64)
N_COUNTIES = len(_CENTROID_NAMES)
del _centroid_df


# ===================================================================
# Phase 1: File Discovery
# ===================================================================

# Regex for CMIP6 tas daily filenames (no fixed character positions)
FILENAME_RE = re.compile(
    r"^tas_day_"
    r"(?P<model>.+)_"
    r"(?P<scenario>ssp\d+)_"
    r"r1i1p1f1_"
    r"(?P<grid>g[rn]\d*)_"
    r"(?P<start>\d{8})-(?P<end>\d{8})"
    r"\.nc$"
)


def discover_files(data_dir: Union[str, Path]) -> List[FileTask]:
    """Walk data directory and discover all processable NetCDF files.

    Skips historical/ directories and empty scenario directories.
    Handles missing scenarios gracefully.
    """
    data_dir = Path(data_dir)
    tasks: list[FileTask] = []

    if not data_dir.exists():
        logger.error("Data directory not found: %s", data_dir)
        return tasks

    for model_dir in sorted(data_dir.iterdir()):
        if not model_dir.is_dir():
            continue

        model = model_dir.name

        # Optional model filter from config
        if MODEL_FILTER and model not in MODEL_FILTER:
            logger.debug("Skipping model (not in MODEL_FILTER): %s", model)
            continue

        for scenario_dir in sorted(model_dir.iterdir()):
            if not scenario_dir.is_dir():
                continue

            scenario = scenario_dir.name

            # Skip historical
            if scenario == "historical":
                logger.debug("Skipping historical: %s/%s", model, scenario)
                continue

            # Only process recognized future scenarios
            if scenario not in VALID_SCENARIOS:
                logger.debug("Skipping unrecognized scenario: %s/%s", model, scenario)
                continue

            nc_files = sorted(scenario_dir.glob("*.nc"))

            if not nc_files:
                logger.info(
                    "No .nc files in %s/%s — data may not be published.",
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
    counts: Counter = Counter()
    for t in tasks:
        counts[(t.model, t.scenario)] += 1

    grid_lookup: dict[tuple[str, str], str] = {}
    for t in tasks:
        key = (t.model, t.scenario)
        if key not in grid_lookup:
            grid_lookup[key] = t.grid

    logger.info("=== File Discovery Summary ===")
    logger.info("Total processable files: %d", len(tasks))
    for (model, scenario), count in sorted(counts.items()):
        cal = MODEL_CALENDAR.get(model, "unknown")
        grid = grid_lookup.get((model, scenario), "?")
        logger.info("  %s / %s  (%s) — %d files, calendar=%s",
                    model, scenario, grid, count, cal)

    # Report expected but missing scenarios for discovered models
    models_found = {t.model for t in tasks}
    missing_pairs = []
    for model in sorted(models_found):
        for s in VALID_SCENARIOS:
            if (model, s) not in counts:
                if (Path(DATA_DIR) / model).exists():
                    missing_pairs.append((model, s))
    for model, scenario in sorted(missing_pairs):
        logger.info("  %s / %s — MISSING (0 files, data may not be published)",
                    model, scenario)


# ===================================================================
# Phase 2: Per-File Processing
# ===================================================================

# --- Date utilities (calendar-aware) ----------------------------------

def parse_yyyymmdd(s: str) -> date:
    """Parse YYYYMMDD string to datetime.date."""
    return date(int(s[:4]), int(s[4:6]), int(s[6:8]))


def _is_leap_year(y: int) -> bool:
    return y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)


def generate_dates(
    start_yyyymmdd: str,
    end_yyyymmdd: str,
    calendar: str,
) -> list[Union[date, str]]:
    """Generate all dates in the inclusive range for the given CMIP6 calendar.

    Returns list of datetime.date for Gregorian-compatible calendars,
    list of str (YYYY-MM-DD) for 360_day.
    """
    if calendar == "360_day":
        return _generate_dates_360(start_yyyymmdd, end_yyyymmdd)

    start = parse_yyyymmdd(start_yyyymmdd)
    end = parse_yyyymmdd(end_yyyymmdd)

    include_leap = (calendar == "proleptic_gregorian")

    dates: list[date] = []
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
    """Generate 360-day calendar dates as YYYY-MM-DD strings.

    12 months × 30 days each. Months 1-12 all have 30 days.
    Dates like Feb 30, Apr 31 are valid in this calendar.
    """
    start_y = int(start_yyyymmdd[:4])
    start_m = int(start_yyyymmdd[4:6])
    start_d = int(start_yyyymmdd[6:8])

    end_y = int(end_yyyymmdd[:4])
    end_m = int(end_yyyymmdd[4:6])
    end_d = int(end_yyyymmdd[6:8])

    dates: list[str] = []
    y, m, d = start_y, start_m, start_d
    while (y < end_y) or (y == end_y and m < end_m) or (y == end_y and m == end_m and d <= end_d):
        dates.append(f"{y:04d}-{m:02d}-{d:02d}")
        d += 1
        if d > 30:
            d = 1
            m += 1
            if m > 12:
                m = 1
                y += 1
    return dates


# --- Coordinate variable discovery -----------------------------------

def _discover_coord_vars(ds) -> tuple[str, str]:
    """Find latitude and longitude variable names in a NetCDF dataset.

    Uses CF conventions: axis attribute, units attribute, then common names.
    """
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


def _task_context(task: FileTask, lat_var: str, lon_var: str,
                  lat_shape, lon_shape) -> str:
    return (
        f"model={task.model}, scenario={task.scenario}, file={task.nc_path}, "
        f"lat_var={lat_var}, lon_var={lon_var}, "
        f"lat_shape={tuple(lat_shape)}, lon_shape={tuple(lon_shape)}"
    )


def validate_1d_rectilinear_grid(ds, lat_var: str, lon_var: str,
                                 task: FileTask) -> tuple[np.ndarray, np.ndarray]:
    """Return validated 1D monotonic latitude and longitude coordinate arrays."""
    if not lat_var or not lon_var:
        raise ValueError(
            "Could not identify latitude/longitude coordinate variables for "
            f"model={task.model}, scenario={task.scenario}, file={task.nc_path}; "
            f"lat_var={lat_var!r}, lon_var={lon_var!r}"
        )

    lat_values = np.asarray(ds[lat_var].values)
    lon_values = np.asarray(ds[lon_var].values)
    context = _task_context(task, lat_var, lon_var,
                            lat_values.shape, lon_values.shape)

    if lat_values.ndim != 1 or lon_values.ndim != 1:
        raise ValueError(
            "Unsupported grid for centroid-contained grid-cell extraction: "
            "latitude and longitude coordinates must both be 1D rectilinear "
            f"arrays. {context}"
        )

    try:
        grid_lats = lat_values.astype(np.float64)
        grid_lons = lon_values.astype(np.float64)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "Unsupported grid for centroid-contained grid-cell extraction: "
            f"latitude/longitude coordinates must be numeric. {context}"
        ) from exc

    if grid_lats.size < 2 or grid_lons.size < 2:
        raise ValueError(
            "Unsupported grid for centroid-contained grid-cell extraction: "
            f"latitude/longitude coordinates must have at least two centers. {context}"
        )

    if not np.isfinite(grid_lats).all() or not np.isfinite(grid_lons).all():
        raise ValueError(
            "Unsupported grid for centroid-contained grid-cell extraction: "
            f"latitude/longitude coordinates contain non-finite values. {context}"
        )

    for coord_name, coord in (("latitude", grid_lats), ("longitude", grid_lons)):
        diffs = np.diff(coord)
        if not (np.all(diffs > 0) or np.all(diffs < 0)):
            raise ValueError(
                "Unsupported grid for centroid-contained grid-cell extraction: "
                f"{coord_name} coordinate is not strictly monotonic. {context}"
            )

    return grid_lats, grid_lons


def compute_1d_cell_bounds(coord: np.ndarray) -> np.ndarray:
    """Infer per-cell [lower, upper] bounds from 1D coordinate centers."""
    coord = np.asarray(coord, dtype=np.float64)
    midpoints = (coord[:-1] + coord[1:]) / 2.0
    edge_start = coord[0] - (coord[1] - coord[0]) / 2.0
    edge_end = coord[-1] + (coord[-1] - coord[-2]) / 2.0
    edges = np.concatenate(([edge_start], midpoints, [edge_end]))
    lower = np.minimum(edges[:-1], edges[1:])
    upper = np.maximum(edges[:-1], edges[1:])
    return np.column_stack((lower, upper))


def normalize_longitudes_to_grid(
    centroid_lons: np.ndarray,
    grid_lons: np.ndarray,
) -> np.ndarray:
    """Normalize centroid longitudes to the grid's longitude convention."""
    clons = np.asarray(centroid_lons, dtype=np.float64).copy()
    grid_min = float(np.nanmin(grid_lons))
    grid_max = float(np.nanmax(grid_lons))

    if grid_min >= 0 and grid_max > 180:
        clons = np.mod(clons, 360.0)
    elif grid_min < 0 and grid_max <= 180:
        clons = ((clons + 180.0) % 360.0) - 180.0

    return clons


def locate_points_in_1d_bounds(points: np.ndarray, bounds: np.ndarray) -> np.ndarray:
    """Locate each point in [lower, upper) intervals; last interval includes upper."""
    points = np.asarray(points, dtype=np.float64)
    lower = bounds[:, 0]
    upper = bounds[:, 1]
    contains = (
        (points[:, np.newaxis] >= lower[np.newaxis, :]) &
        (points[:, np.newaxis] < upper[np.newaxis, :])
    )
    contains[:, -1] |= (
        (points >= lower[-1]) &
        (points <= upper[-1])
    )
    has_match = contains.any(axis=1)
    result = np.argmax(contains, axis=1).astype(np.int64)
    result[~has_match] = -1
    return result


def _format_unassigned(names: list[str], lons: np.ndarray, lats: np.ndarray,
                       norm_lons: np.ndarray, bad: np.ndarray,
                       max_items: int = 10) -> str:
    parts = []
    for i in bad[:max_items]:
        j = int(i)
        parts.append(
            f"{names[j]}(lon={lons[j]}, lat={lats[j]}, "
            f"normalized_lon={norm_lons[j]})"
        )
    if len(bad) > max_items:
        parts.append(f"... {len(bad) - max_items} more")
    return "; ".join(parts)


def match_centroids_to_rectilinear_grid_cells(
    centroid_names: list[str],
    centroid_lons: np.ndarray,
    centroid_lats: np.ndarray,
    grid_lats: np.ndarray,
    grid_lons: np.ndarray,
    task: FileTask,
    lat_var: str,
    lon_var: str,
) -> dict[str, np.ndarray]:
    """Assign centroids to containing 1D rectilinear grid cells."""
    lat_bounds = compute_1d_cell_bounds(grid_lats)
    lon_bounds = compute_1d_cell_bounds(grid_lons)
    normalized_lons = normalize_longitudes_to_grid(centroid_lons, grid_lons)
    lon_points = normalized_lons.copy()

    # Global longitude grids can have an inferred edge just outside their
    # display convention, e.g. [-0.5, 0.5] around a 0-degree center. Shift only
    # points that cross that seam for containment lookup; keep the audit column
    # in the grid's ordinary longitude convention.
    if (np.nanmax(grid_lons) - np.nanmin(grid_lons)) >= 300:
        min_bound = np.nanmin(lon_bounds[:, 0])
        max_bound = np.nanmax(lon_bounds[:, 1])
        if min_bound < np.nanmin(grid_lons):
            lon_points = np.where(lon_points > max_bound,
                                  lon_points - 360.0, lon_points)
        if max_bound > np.nanmax(grid_lons):
            lon_points = np.where(lon_points < min_bound,
                                  lon_points + 360.0, lon_points)

    lat_idx = locate_points_in_1d_bounds(centroid_lats, lat_bounds)
    lon_idx = locate_points_in_1d_bounds(lon_points, lon_bounds)

    bad = np.where((lat_idx < 0) | (lon_idx < 0))[0]
    if bad.size:
        context = _task_context(task, lat_var, lon_var,
                                grid_lats.shape, grid_lons.shape)
        raise ValueError(
            "County centroid could not be assigned to a rectilinear grid-cell "
            f"boundary interval. {context}. Unassigned counties: "
            f"{_format_unassigned(centroid_names, centroid_lons, centroid_lats, normalized_lons, bad)}"
        )

    return {
        "lat_idx": lat_idx,
        "lon_idx": lon_idx,
        "normalized_lons": normalized_lons,
        "lat_centers": grid_lats[lat_idx],
        "lon_centers": grid_lons[lon_idx],
        "lat_bounds": lat_bounds[lat_idx],
        "lon_bounds": lon_bounds[lon_idx],
    }


def write_grid_audit_table(
    task: FileTask,
    match: dict[str, np.ndarray],
    audit_dir: Path,
) -> str:
    """Write a per-file county-to-grid audit table."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    audit_dir.mkdir(parents=True, exist_ok=True)
    n_counties = len(_CENTROID_NAMES)
    table = pa.table({
        "county": pa.array(_CENTROID_NAMES, type=pa.string()),
        "centroid_lon": pa.array(_CENTROID_LONS, type=pa.float64()),
        "centroid_lat": pa.array(_CENTROID_LATS, type=pa.float64()),
        "normalized_centroid_lon": pa.array(match["normalized_lons"], type=pa.float64()),
        "grid_lat_idx": pa.array(match["lat_idx"], type=pa.int32()),
        "grid_lon_idx": pa.array(match["lon_idx"], type=pa.int32()),
        "grid_lat_center": pa.array(match["lat_centers"], type=pa.float64()),
        "grid_lon_center": pa.array(match["lon_centers"], type=pa.float64()),
        "grid_lat_bound_min": pa.array(match["lat_bounds"][:, 0], type=pa.float64()),
        "grid_lat_bound_max": pa.array(match["lat_bounds"][:, 1], type=pa.float64()),
        "grid_lon_bound_min": pa.array(match["lon_bounds"][:, 0], type=pa.float64()),
        "grid_lon_bound_max": pa.array(match["lon_bounds"][:, 1], type=pa.float64()),
        "model": pa.array([task.model] * n_counties, type=pa.string()),
        "scenario": pa.array([task.scenario] * n_counties, type=pa.string()),
        "grid": pa.array([task.grid] * n_counties, type=pa.string()),
    })

    audit_path = audit_dir / (
        f"{task.model}_{task.scenario}_"
        f"{task.start_yyyymmdd}_{task.end_yyyymmdd}_grid_audit.parquet"
    )
    pq.write_table(table, str(audit_path), compression="snappy")
    logger.debug("  Wrote grid audit table: %s (%d rows)",
                 audit_path.name, len(table))
    return str(audit_path)


# --- Centroid-to-grid matching ---------------------------------------

def _match_centroids_to_grid(
    centroid_lons: np.ndarray,
    centroid_lats: np.ndarray,
    grid_lats: np.ndarray,
    grid_lons: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Deprecated helper retained only to prevent accidental nearest matching."""
    raise RuntimeError(
        "Nearest-center centroid matching is disabled. Use "
        "match_centroids_to_rectilinear_grid_cells() for boundary containment."
    )


# --- Single-file processor -------------------------------------------

def process_one_file(
    task: FileTask,
    temp_parquet_dir: Path = TEMP_PARQUET_DIR,
) -> Optional[str]:
    """Process one NetCDF file via centroid-contained grid-cell extraction.

    Returns the path to the temp Parquet, or None on failure.
    """
    import xarray as xr
    import pyarrow as pa
    import pyarrow.parquet as pq

    nc_name = Path(task.nc_path).name
    logger.info("Processing: %s", nc_name)
    logger.info("  Method: centroid-contained grid-cell extraction")

    if len(_CENTROID_NAMES) == 0:
        raise RuntimeError("County centroids not loaded. Check COUNTY_COORDINATE_FILE.")

    calendar = MODEL_CALENDAR.get(task.model, "365_day")

    # Step 1: Generate dates from filename date range
    dates = generate_dates(task.start_yyyymmdd, task.end_yyyymmdd, calendar)
    n_dates = len(dates)
    logger.debug("  Date range: %s – %s (%d dates, calendar=%s)",
                 task.start_yyyymmdd, task.end_yyyymmdd, n_dates, calendar)

    # Step 2: Open NetCDF and extract grid + data
    ds = xr.open_dataset(task.nc_path, decode_times=False)
    time_size = ds.sizes.get("time", 0)
    ds.close()

    if time_size != n_dates:
        logger.warning(
            "  Time dimension mismatch: NetCDF has %d steps, "
            "filename implies %d dates.", time_size, n_dates)
        if time_size < n_dates:
            dates = dates[:time_size]
            n_dates = time_size

    ds = xr.open_dataset(task.nc_path)
    tas = ds["tas"].values.astype(np.float32)

    lat_var, lon_var = _discover_coord_vars(ds)
    grid_lats, grid_lons = validate_1d_rectilinear_grid(
        ds, lat_var, lon_var, task)
    match = match_centroids_to_rectilinear_grid_cells(
        _CENTROID_NAMES, _CENTROID_LONS, _CENTROID_LATS,
        grid_lats, grid_lons, task, lat_var, lon_var)
    ds.close()

    logger.debug("  Grid shape: %s, %d time steps, coords: %s/%s",
                 tas.shape, n_dates, lat_var, lon_var)
    logger.debug("  Coordinate centers: lat=%d, lon=%d",
                 len(grid_lats), len(grid_lons))

    # Step 3: Write audit table and extract centroid-containing grid cells
    audit_dir = temp_parquet_dir.parent / TEMP_AUDIT_DIR.name
    write_grid_audit_table(task, match, audit_dir)
    lat_idx = match["lat_idx"]
    lon_idx = match["lon_idx"]

    # Step 4: Extract time series for each county
    county_tas = tas[:, lat_idx, lon_idx].T.astype(np.float32)
    n_counties = len(_CENTROID_NAMES)

    logger.debug("  Extracted %d counties × %d days", n_counties, n_dates)

    # Step 5: Build long-form pyarrow Table
    dates_col = np.repeat(dates, n_counties)
    names_col = np.tile(np.array(_CENTROID_NAMES, dtype=object), n_dates)
    tas_col = county_tas.T.ravel()

    # Choose date type based on calendar
    is_360 = (calendar == "360_day")
    date_type = pa.string() if is_360 else pa.date32()

    table = pa.table({
        "date": pa.array(dates_col, type=date_type),
        "county": pa.array(names_col, type=pa.string()),
        "tas_centroid_grid_k": pa.array(tas_col, type=pa.float32()),
    })

    # Step 6: Write temp Parquet
    temp_path = temp_parquet_dir / (
        f"{task.model}_{task.scenario}_"
        f"{task.start_yyyymmdd}_{task.end_yyyymmdd}.parquet"
    )
    pq.write_table(table, str(temp_path), compression="snappy")
    total_rows = len(table)
    del table
    logger.info("  Wrote temp Parquet: %s (%d rows)", temp_path.name, total_rows)

    gc.collect()
    return str(temp_path)


# --- Batch processor -------------------------------------------------

def process_all_files(
    tasks: list[FileTask],
    temp_parquet_dir: Path = TEMP_PARQUET_DIR,
) -> dict[tuple[str, str], list[str]]:
    """Process all discovered files. Returns mapping (model, scenario) → temp paths."""
    results: dict[tuple[str, str], list[str]] = {}
    summary = ProcessingSummary()

    # Ensure temp dir exists
    temp_parquet_dir.mkdir(parents=True, exist_ok=True)

    total = len(tasks)
    for idx, task in enumerate(tasks, 1):
        key = (task.model, task.scenario)
        if key not in results:
            results[key] = []

        try:
            logger.info("[%d/%d] %s / %s / %s",
                        idx, total, task.model, task.scenario,
                        Path(task.nc_path).name)
            temp_path = process_one_file(task, temp_parquet_dir)
            if temp_path:
                results[key].append(temp_path)
                summary.succeeded += 1
            else:
                summary.failed += 1
                summary.failed_files.append(task.nc_path)
        except Exception:
            logger.exception("FAILED: %s", task.nc_path)
            summary.failed += 1
            summary.failed_files.append(task.nc_path)
            gc.collect()
            continue

        if idx % 10 == 0:
            gc.collect()

    logger.info("=== Processing phase complete ===")
    logger.info("Succeeded: %d, Failed: %d, Total: %d",
                summary.succeeded, summary.failed, total)
    if summary.failed_files:
        logger.warning("Failed files:")
        for f in summary.failed_files:
            logger.warning("  - %s", f)

    return results


# ===================================================================
# Phase 3: Assembly
# ===================================================================

def assemble_model_scenario(
    model: str,
    scenario: str,
    temp_paths: list[str],
    output_dir: Path = OUTPUT_DIR,
) -> str:
    """Concatenate temp Parquet files into the final model-scenario output."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    temp_paths = sorted(temp_paths)

    output_path = output_dir / f"{model}_{scenario}_county_daily_tas.parquet"
    logger.info("Assembling: %s (%d temp files)", output_path.name, len(temp_paths))

    first_table = pq.read_table(temp_paths[0])
    schema = first_table.schema
    total_rows = 0

    with pq.ParquetWriter(str(output_path), schema=schema) as writer:
        writer.write_table(first_table)
        total_rows += len(first_table)
        del first_table

        for temp_path in temp_paths[1:]:
            table = pq.read_table(temp_path)
            writer.write_table(table)
            total_rows += len(table)
            del table

    logger.info("  Assembly complete: %d rows", total_rows)
    return str(output_path)


def assemble_grid_audit(
    model: str,
    scenario: str,
    audit_temp_dir: Path = TEMP_AUDIT_DIR,
    output_dir: Path = OUTPUT_DIR,
) -> Optional[str]:
    """Assemble per-file county-to-grid audit tables for a model-scenario."""
    import pyarrow.parquet as pq

    if not audit_temp_dir.exists():
        logger.warning("No audit temp directory found: %s", audit_temp_dir)
        return None

    audit_paths = sorted(audit_temp_dir.glob(
        f"{model}_{scenario}_*_grid_audit.parquet"))
    if not audit_paths:
        logger.warning("No grid audit temp files for %s/%s.", model, scenario)
        return None

    audit_frames = [pq.read_table(str(p)).to_pandas() for p in audit_paths]
    audit_df = pd.concat(audit_frames, ignore_index=True)
    audit_df = audit_df.drop_duplicates().sort_values(
        ["model", "scenario", "grid", "county"]).reset_index(drop=True)

    audit_output_dir = output_dir / AUDIT_OUTPUT_DIR.name
    audit_output_dir.mkdir(parents=True, exist_ok=True)
    audit_output_path = (
        audit_output_dir / f"{model}_{scenario}_county_grid_audit.parquet"
    )
    audit_df.to_parquet(str(audit_output_path), index=False)
    logger.info("  Grid audit assembled: %s (%d rows)",
                audit_output_path, len(audit_df))

    for audit_path in audit_paths:
        try:
            audit_path.unlink()
        except Exception:
            pass

    return str(audit_output_path)


def assemble_all(
    results: dict[tuple[str, str], list[str]],
    output_dir: Path = OUTPUT_DIR,
) -> list[str]:
    """Assemble all model-scenario pairs. Returns list of final output paths."""
    output_dir.mkdir(parents=True, exist_ok=True)
    output_paths: list[str] = []

    for (model, scenario), temp_paths in sorted(results.items()):
        if not temp_paths:
            logger.warning("No temp files for %s/%s, skipping assembly.",
                           model, scenario)
            continue

        output_path = assemble_model_scenario(model, scenario, temp_paths, output_dir)
        output_paths.append(output_path)
        assemble_grid_audit(model, scenario, output_dir=output_dir)

        for temp_path in temp_paths:
            try:
                Path(temp_path).unlink()
            except Exception:
                pass

    return output_paths


# ===================================================================
# Phase 4: Cleanup
# ===================================================================

def cleanup_temp_files() -> None:
    """Remove temporary Parquet directory."""
    logger.info("=== Cleanup ===")
    if TEMP_PARQUET_DIR.exists():
        shutil.rmtree(str(TEMP_PARQUET_DIR), ignore_errors=True)
        logger.info("Temp Parquet directory removed.")
    if TEMP_AUDIT_DIR.exists():
        shutil.rmtree(str(TEMP_AUDIT_DIR), ignore_errors=True)
        logger.info("Temp grid audit directory removed.")

    try:
        remaining = list(TEMP_DIR.iterdir())
        if not remaining:
            shutil.rmtree(str(TEMP_DIR), ignore_errors=True)
        else:
            logger.debug("Temp directory not empty, keeping: %s",
                         [f.name for f in remaining])
    except Exception:
        pass


# ===================================================================
# CLI
# ===================================================================

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="CMIP6 county-level centroid-contained grid-cell temperature processing")
    parser.add_argument(
        "--model", type=str, default=None,
        help="Process only this model (e.g. FGOALS-g3). If omitted, process all.")
    parser.add_argument(
        "--scenario", type=str, default=None,
        choices=VALID_SCENARIOS,
        help="Process only this scenario. Requires --model.")
    parser.add_argument(
        "--skip-assembly", action="store_true",
        help="Skip final assembly; only produce temp Parquet files.")
    parser.add_argument(
        "--assemble-only", action="store_true",
        help="Skip processing; assemble outputs from existing temp files.")
    parser.add_argument(
        "--no-cleanup", action="store_true",
        help="Do not delete temp files after assembly.")
    return parser


def _group_existing_temp_files(temp_dir: Path) -> dict[tuple[str, str], list[str]]:
    """Discover existing temp Parquet files and group by (model, scenario)."""
    if not temp_dir.exists():
        logger.error("Temp directory not found: %s", temp_dir)
        return {}

    results: dict[tuple[str, str], list[str]] = {}
    for p in sorted(temp_dir.glob("*.parquet")):
        parts = p.stem.split("_")
        # Pattern: MODEL_SCENARIO_START_END
        if len(parts) >= 2:
            model = parts[0]
            scenario = parts[1]
            results.setdefault((model, scenario), []).append(str(p))
    return results


def main(passed_args: Optional[List[str]] = None) -> int:
    """Main orchestrator. Returns 0 on success, 1 on error."""
    if passed_args is None:
        passed_args = sys.argv[1:]

    parser = build_arg_parser()
    args = parser.parse_args(passed_args)

    _setup_file_log(PROJECT_DIR / "process.log")
    logger.info("CMIP6 County-Level Centroid-Contained Grid-Cell Temperature Processing")
    logger.info("Project: %s", PROJECT_DIR)
    logger.info("Counties loaded: %d (from %s)", N_COUNTIES, COUNTY_COORDINATE_FILE)

    # sleep_for_hours(2)

    # --- Assemble-only mode ---
    if args.assemble_only:
        logger.info("=== Assemble-Only Mode ===")
        results = _group_existing_temp_files(TEMP_PARQUET_DIR)
        if not results:
            logger.error("No temp Parquet files found in %s", TEMP_PARQUET_DIR)
            return 1
        for (m, s), paths in sorted(results.items()):
            logger.info("  %s/%s: %d temp files", m, s, len(paths))
        output_paths = assemble_all(results)
        logger.info("=== Done ===")
        for p in output_paths:
            fsize = Path(p).stat().st_size / (1024 ** 3)
            logger.info("  %s (%.2f GB)", p, fsize)
        return 0

    # --- Phase 1: Discover ---
    logger.info("=== Phase 1: File Discovery ===")
    tasks = discover_files(DATA_DIR)

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

    # --- Phase 2: Process ---
    logger.info("=== Phase 2: Processing ===")
    logger.info("Processing %d files across %d model-scenario pairs.",
                len(tasks),
                len({(t.model, t.scenario) for t in tasks}))

    try:
        results = process_all_files(tasks)

        # --- Phase 3: Assemble ---
        if not args.skip_assembly:
            logger.info("=== Phase 3: Assembly ===")
            output_paths = assemble_all(results)
            logger.info("Output files:")
            for p in output_paths:
                logger.info("  %s", p)
        else:
            output_paths = []
            logger.info("Assembly skipped (--skip-assembly).")

    except KeyboardInterrupt:
        logger.warning("Interrupted by user.")
        output_paths = []

    # --- Phase 4: Cleanup ---
    if not args.no_cleanup:
        cleanup_temp_files()
    else:
        logger.info("Cleanup skipped (--no-cleanup). Temp files kept.")

    logger.info("=== Done ===")
    logger.info("Output Parquet files: %d", len(output_paths))
    for p in output_paths:
        fsize = Path(p).stat().st_size / (1024 ** 3)
        logger.info("  %s (%.2f GB)", p, fsize)

    return 0


if __name__ == "__main__":
    sys.exit(main())
