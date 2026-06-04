# CMIP6 County-Level Daily Temperature

Processes CMIP6 daily near-surface air temperature (`tas`) NetCDF files into
county-level daily Parquet tables.

Two extraction methods are available:

1. **Centroid-based** (`code/process_cmip6.py`): Each county is assigned to the
   grid cell containing its centroid. (Superseded for this task.)

2. **Overlap-weighted** (`code/process_cmip6_overlap.py`): County temperature is
   the area-weighted average of all grid cells intersecting the county polygon.
   This is the preferred method.

## Quick Start (Overlap-Weighted — Recommended)

1. Configure `code/config.py`: paths, scenarios, optional model filter, and model calendars.
2. Place data under `data/{ModelName}/{scenario}/`.
3. Place county boundaries at `data/boundary/xian_rename.shp`.
4. Run the CLI in the Agent conda environment:

```bash
# Full pipeline: weights + extraction + assembly
python code/process_cmip6_overlap.py

# Single model
python code/process_cmip6_overlap.py --model FGOALS-g3

# Single model-scenario
python code/process_cmip6_overlap.py --model GFDL-CM4 --scenario ssp245

# Only compute weight tables (no extraction)
python code/process_cmip6_overlap.py --weights-only

# Skip weight computation (use existing)
python code/process_cmip6_overlap.py --skip-weights

# Keep temp files for debugging
python code/process_cmip6_overlap.py --no-cleanup
```

`--scenario` requires `--model`.

### Output

```text
output_overlap/weights/{model}_{grid_label}_overlap_weights.parquet
output_overlap/final/{model}_{scenario}_county_daily_tas_overlap.parquet
```

| Column | Type | Description |
|---|---|---|
| `date` | `date32` or `string` | `string` used only for `360_day` calendars |
| `NAME` | `string` | County name from shapefile |
| `tas_mean_k` | `float32` | Area-weighted daily temperature in Kelvin |

## Quick Start (Centroid-Based — Legacy)

```bash
python code/process_cmip6.py
python code/process_cmip6.py --model FGOALS-g3
python code/process_cmip6.py --model GFDL-CM4 --scenario ssp245
python code/process_cmip6.py --skip-assembly --no-cleanup
python code/process_cmip6.py --assemble-only
```

Output: `output/{model}_{scenario}_county_daily_tas.parquet`

## Cross-Model Mean

After per-model tables are generated, compute scenario-level cross-model means:

```bash
# For overlap-weighted outputs
python code/compute_model_mean.py --overlap

# For centroid-based outputs (legacy)
python code/compute_model_mean.py
```

This produces 6 output tables (3 scenarios x 2 IPSL-CM6A-LR inclusion versions):

```text
{output_dir}/{scenario}_model_mean_{with,without}_IPSL-CM6A-LR.parquet
```

| Column | Type | Description |
|---|---|---|
| `date` | `string` | Date in YYYY-MM-DD format |
| `NAME` | `string` | County identifier |
| `tas_mean_k` | `float64` | Cross-model mean daily temperature in Kelvin |
| `n_candidate_models` | `int64` | Number of non-missing model values used |

The mean denominator is dynamic per county-date because CMIP6 models use
different calendars (360_day, 365_day, noleap, proleptic_gregorian). Not
every model has every nominal date. `n_candidate_models` records the exact
number of contributing models for each row.

GFDL-CM4 is absent from ssp126 (no source data). This is handled gracefully.

See `Mean_Cal.md` for full details.

## Method (Overlap-Weighted)

1. Discover future-scenario NetCDF files under `DATA_DIR/{model}/{scenario}/`.
2. Catalog each unique (model, grid_label) grid by extracting 1D lat/lon coordinates.
3. For each model-grid, build rectangular grid cell polygons from inferred cell bounds.
4. Load county boundary polygons from `data/boundary/xian_rename.shp`.
5. Compute county-cell overlap weights using geodesic area (pyproj.Geodesic).
6. Save per-model-grid weight tables to `output_overlap/weights/`.
7. For each NetCDF file, build a sparse weight matrix and extract daily tas.
8. Compute `county_tas = sum(weight_i * tas_i)` for all intersecting cells.
9. Assemble per-file outputs into model-scenario Parquet files under `output_overlap/final/`.

Overlap weights are computed **once per model-grid** and reused across all
scenarios and time slices. See `plan_overlap.md` for full details.

## Method (Centroid-Based — Legacy)

1. Load county centroids from `data/CountyCoordinate.dta`.
2. Discover future-scenario NetCDF files under `DATA_DIR/{model}/{scenario}/`.
3. For each file, discover latitude and longitude coordinate variables.
4. Require both coordinate arrays to be one-dimensional, numeric, finite, and strictly monotonic.
5. Infer grid-cell boundaries from adjacent coordinate centers using midpoint boundaries.
6. Normalize centroid longitudes to the grid convention, such as 0-360 when needed.
7. Assign each county centroid to the latitude and longitude intervals that contain it.
8. Extract `tas[:, lat_idx, lon_idx]` from the centroid-containing grid cell.

Unsupported 2D, curvilinear, non-monotonic, or otherwise incompatible grids are
rejected with a clear error. The script does not fall back to nearest grid-center
matching.

## Output

```text
output/{model}_{scenario}_county_daily_tas.parquet
```

| Column | Type | Description |
|---|---|---|
| `date` | `date32` or `string` | `string` is used only for `360_day` calendars |
| `county` | `string` | County name from `CountyCoordinate.dta` |
| `tas_centroid_grid_k` | `float32` | Daily `tas` value from the centroid-containing grid cell |

Audit files are written during assembly:

```text
output/grid_audit/{model}_{scenario}_county_grid_audit.parquet
```

The audit table records county centroid coordinates, normalized longitude,
selected grid indices, selected grid centers, selected bounds, model, scenario,
and grid label.

## Configuration

All settings are in `code/config.py`. Key entries:

```python
DATA_DIR = PROJECT_DIR / "data"
OUTPUT_DIR = PROJECT_DIR / "output"
TEMP_DIR = PROJECT_DIR / "temp"
COUNTY_COORDINATE_FILE = PROJECT_DIR / "data/CountyCoordinate.dta"
VALID_SCENARIOS = ("ssp126", "ssp245", "ssp585")
MODEL_FILTER = None
MODEL_CALENDAR = {
    "FGOALS-g3": "365_day",
    "GFDL-CM4": "noleap",
}
```

If a discovered model has no calendar entry, the script falls back to
`365_day`. Add an explicit entry to avoid silently using the wrong calendar.

## Temporary Files

The CLI uses `TEMP_DIR/parquet_temps` for data temps and
`TEMP_DIR/grid_audit_temps` for audit temps. `--skip-assembly` still performs
cleanup unless combined with `--no-cleanup`.

## ArcGIS Pro Toolbox

`cmip6_toolbox.pyt` is an optional ArcGIS Pro GUI wrapper. It imports and calls
`code/process_cmip6.py` inside the ArcGIS Pro Python process, so that environment
needs working pandas, NumPy, xarray, PyArrow, and NetCDF backend packages in
addition to ArcPy.

Neither `code/process_cmip6.py` nor `cmip6_toolbox.pyt` creates a `scratch.gdb`.

## Supported Calendars

| Calendar | Models configured in `code/config.py` |
|---|---|
| `365_day` | FGOALS-g3, INM-CM5-0, INM-CM4-8 |
| `noleap` | NorESM2-MM, NorESM2-LM, TaiESM1, GFDL-CM4, IPSL-CM6A-LR, CanESM5 |
| `proleptic_gregorian` | MRI-ESM2-0, MPI-ESM1-2-HR |
| `360_day` | KACE-1-0-G |
