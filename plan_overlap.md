# plan_overlap.md — Overlap-Weighted County Temperature Extraction

## Overview

This pipeline computes county-level daily near-surface air temperature (`tas`)
from CMIP6 model outputs using **polygon-grid-cell area overlap weighting**.
It replaces the previous centroid-based extraction method.

## How It Differs from the Centroid Method

| Aspect | Centroid Method | Overlap-Weighted Method |
|---|---|---|
| County representation | Single (lon, lat) point | Full polygon boundary |
| Grid cell assignment | One cell containing the centroid | All intersecting cells |
| Temperature formula | `tas` of the containing cell | Weighted average: `sum(w_i * tas_i)` |
| Weight basis | Binary (1 for containing cell) | Overlap area / county area |
| Accuracy for large/border counties | Coarse approximation | Physically correct spatial average |

## Input Data

### County Boundary Polygons
- **Path:** `data/boundary/xian_rename.shp`
- **CRS:** WGS84 (EPSG:4326)
- **Key field:** `NAME` (county name, Chinese characters)

### CMIP6 NetCDF Files
- **Location:** `data/{model}/{scenario}/tas_day_*.nc`
- **Variable:** `tas` — daily near-surface air temperature, Kelvin
- **Grids:** 1D rectilinear lat/lon coordinates, native model grids (gn, gr, gr1, gr2)
- **Scenarios:** ssp126, ssp245, ssp585 (future projections)
- **Models:** 12 CMIP6 GCMs (see `CMIP6 data explanation.txt`)
- **Calendars:** 360_day, 365_day, noleap, proleptic_gregorian

## Output Directory Structure

```
output_overlap/
  weights/        One Parquet per unique (model, grid_label)
  final/          Per-model per-scenario county-daily temperature Parquets
  logs/           Processing logs, QC reports, weight-issue summaries
  temp/           Temporary per-file Parquets (cleaned up unless --no-cleanup)
```

## Workflow

### Phase 1: File Discovery
- Walk `data/{model}/{scenario}/` for `tas_day_*.nc` files
- Parse CMIP6 naming convention via regex (model, scenario, grid, date range)
- Skip historical and unrecognized scenarios

### Phase 2: Grid Registry
- Group files by `(model, grid_label)` since the grid is invariant
- Open one representative file per model-grid
- Extract and validate 1D monotonic lat/lon coordinate arrays
- Example: GFDL-CM4 uses `gr1` for historical and `gr2` for SSP → two separate grid entries

### Phase 3: Overlap Weight Computation (once per model-grid)

This is the key optimization: weights are computed **once per model-grid** and reused across all scenarios and time slices.

1. **Load county polygons** from `data/boundary/xian_rename.shp`
2. **Build grid cell polygons** from 1D coordinate centers:
   - Compute cell bounds using midpoint boundaries between adjacent centers
   - Create rectangular `shapely.geometry.box()` polygons for each cell
   - Size: `nlat * nlon` polygons
3. **Compute county geodesic areas** using `pyproj.Geodesic` (Karney's algorithm on WGS84 ellipsoid)
4. **Find intersections** using R-tree spatial index:
   - For each county, query intersecting grid cells
   - Compute intersection geometry
   - Calculate intersection geodesic area
   - `overlap_weight = area(intersection) / area(county)`
5. **Save weight table** to `output_overlap/weights/{model}_{grid_label}_overlap_weights.parquet`

#### Weight Table Schema

| Column | Type | Description |
|---|---|---|
| model | string | CMIP6 model name |
| grid_label | string | Grid variant (gn, gr, gr1, gr2) |
| NAME | string | County name from shapefile |
| lat_idx | int64 | 0-based index into grid lat array |
| lon_idx | int64 | 0-based index into grid lon array |
| grid_lat_center | float64 | Grid cell center latitude (degrees) |
| grid_lon_center | float64 | Grid cell center longitude (degrees) |
| overlap_area_m2 | float64 | Area of county-cell intersection (m^2) |
| county_area_m2 | float64 | Total county area (m^2) |
| overlap_weight | float64 | overlap_area / county_area |

#### Why Compute Weights Once Per Model-Grid?

For a given CMIP6 model, the spatial grid (lat/lon coordinates) is identical across:
- All scenarios (ssp126, ssp245, ssp585)
- All time slices within each scenario
- The entire date range (2015–2100)

The only exception in this dataset is GFDL-CM4, which uses `gr1` for historical and `gr2` for SSP. Since we only process SSP scenarios, this means 13 unique (model, grid_label) pairs across 12 models.

### Phase 4: Temperature Extraction

For each NetCDF file:
1. Load the precomputed weight table for `(model, grid_label)`
2. Build a **sparse CSR weight matrix** `W` of shape `(n_counties, nlat * nlon)`
3. Open the NetCDF file and read `tas` in time chunks (~365 days)
4. For each time step: `weighted_temps = W @ tas_2d.ravel()`
   - This is a single sparse matrix-vector multiplication
   - Very fast (< 1ms per time step for typical grids)
5. Write temp Parquet with columns: `date`, `NAME`, `tas_mean_k`

#### Date Handling

| Calendar | Models | Date Storage |
|---|---|---|
| 360_day | KACE-1-0-G | `pa.string()` (YYYY-MM-DD) |
| 365_day | FGOALS-g3, INM-CM5-0, INM-CM4-8 | `pa.date32()` |
| noleap | NorESM2-MM, NorESM2-LM, TaiESM1, GFDL-CM4, IPSL-CM6A-LR, CanESM5 | `pa.date32()` |
| proleptic_gregorian | MRI-ESM2-0, MPI-ESM1-2-HR | `pa.date32()` |

### Phase 5: Assembly and QC

**Assembly:** Concatenate per-file temp Parquets into model-scenario final outputs:
- `output_overlap/final/{model}_{scenario}_county_daily_tas_overlap.parquet`

**QC Checks:**
1. **Weight sum verification:** For each county-model, overlap weights sum to ~1.0. Counties with sums < 0.99 are flagged (partial domain coverage at borders).
2. **Zero-coverage counties:** Counties with no overlapping grid cells are flagged.
3. **Output row counts:** Verified as `n_counties × n_dates` per model-scenario.
4. **Temperature plausibility:** Values must be within 200–330 K for near-surface air temperature.
5. **NaN detection:** Any NaN values in final outputs are flagged.

## Area Calculation Method

**Geodesic area** via `pyproj.Geodesic` is used for all area calculations.

Reasoning:
- No projection distortion (works directly on WGS84 ellipsoid)
- Handles arbitrary polygon shapes correctly (Karney's algorithm)
- Avoids choosing a suitable equal-area projection for China's extent
- `pyproj` is already installed as a dependency of `geopandas`

## Grid Cell Polygon Construction

From 1D lat/lon center coordinates:
1. Cell boundaries are midpoints between adjacent centers
2. Outer edges are extrapolated by half the adjacent spacing
3. Rectangular polygons: `box(lon_min, lat_min, lon_max, lat_max)`
4. All CMIP6 grids in this dataset use 0–360 longitude convention

## Compatibility with compute_model_mean.py

`compute_model_mean.py` has been updated with a `--overlap` flag:

```bash
# Centroid pipeline (original)
python code/compute_model_mean.py

# Overlap pipeline (new)
python code/compute_model_mean.py --overlap
```

When `--overlap` is set:
- Reads from `output_overlap/final/` instead of `output/`
- Matches `*_county_daily_tas_overlap.parquet` instead of `*_county_daily_tas.parquet`
- Uses column `tas_mean_k` instead of `tas_centroid_grid_k`
- Writes cross-model mean outputs to `output_overlap/final/`

## How to Run

```bash
# Full pipeline: weights + extraction + assembly
python code/process_cmip6_overlap.py

# Single model
python code/process_cmip6_overlap.py --model FGOALS-g3

# Single model-scenario
python code/process_cmip6_overlap.py --model FGOALS-g3 --scenario ssp245

# Only compute weight tables (no extraction)
python code/process_cmip6_overlap.py --weights-only

# Skip weight computation (use existing weight tables)
python code/process_cmip6_overlap.py --skip-weights

# Keep temp files for debugging
python code/process_cmip6_overlap.py --no-cleanup

# Only assemble from existing temp files
python code/process_cmip6_overlap.py --assemble-only

# Cross-model mean aggregation
python code/compute_model_mean.py --overlap
```

## Model Grids Summary

| Model | Grid Label | Lat × Lon | Calendar |
|---|---|---|---|
| KACE-1-0-G | gr | TBD | 360_day |
| NorESM2-MM | gn | TBD | noleap |
| NorESM2-LM | gn | TBD | noleap |
| INM-CM5-0 | gr1 | TBD | 365_day |
| INM-CM4-8 | gr1 | TBD | 365_day |
| TaiESM1 | gn | TBD | noleap |
| MRI-ESM2-0 | gn | TBD | proleptic_gregorian |
| MPI-ESM1-2-HR | gn | TBD | proleptic_gregorian |
| IPSL-CM6A-LR | gr | TBD | noleap |
| GFDL-CM4 | gr2 (SSP) | 90 × 144 | noleap |
| CanESM5 | gn | 64 × 128 | noleap |
| FGOALS-g3 | gn | TBD | 365_day |

Grid dimensions are logged at runtime from the grid registry.

## Dependencies

New packages required (added to the Agent environment):
- geopandas >= 1.1
- shapely >= 2.1
- pyproj >= 3.7
- rtree >= 1.4
