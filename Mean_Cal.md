# Mean_Cal.md - Cross-Model Mean Daily Temperature

## Overview

This step aggregates per-model, per-scenario county-level daily temperature
Parquet tables into scenario-level cross-model mean tables. For each scenario,
two versions are produced: one including `IPSL-CM6A-LR` and one excluding it.

## Input

Per-model Parquet files produced by `code/process_cmip6.py`:

```text
output/{model}_{scenario}_county_daily_tas.parquet
```

| Column | Type | Description |
|---|---|---|
| `date` | `datetime.date` or `string` | Daily date; `string` only for 360_day (KACE-1-0-G) |
| `county` | `string` | County identifier |
| `tas_centroid_grid_k` | `float32` | Daily tas in Kelvin from the centroid-containing grid cell |

## Output

Six scenario-level Parquet files:

```text
output/ssp126_model_mean_with_IPSL-CM6A-LR.parquet
output/ssp126_model_mean_without_IPSL-CM6A-LR.parquet
output/ssp245_model_mean_with_IPSL-CM6A-LR.parquet
output/ssp245_model_mean_without_IPSL-CM6A-LR.parquet
output/ssp585_model_mean_with_IPSL-CM6A-LR.parquet
output/ssp585_model_mean_without_IPSL-CM6A-LR.parquet
```

### Output Schema

| Column | Type | Description |
|---|---|---|
| `date` | `string` | Date in YYYY-MM-DD format |
| `PAC` | `string` | County administrative code (overlap mode) / `NAME` (centroid mode) |
| `tas_mean_k` | `float64` | Cross-model mean daily temperature in Kelvin |
| `n_candidate_models` | `int64` | Number of non-missing model values for that county-date |

## Scenarios

| Scenario | Models available | Notes |
|---|---|---|
| `ssp126` | 11 | GFDL-CM4 has no ssp126 data |
| `ssp245` | 12 | All models present |
| `ssp585` | 12 | All models present |

## IPSL-CM6A-LR Versions

For each scenario, two parallel outputs are produced:

1. **With IPSL-CM6A-LR**: Uses all available models for that scenario.
2. **Without IPSL-CM6A-LR**: Excludes IPSL-CM6A-LR from the aggregation.

The exclusion is implemented as a simple file-list filter in `code/compute_model_mean.py`.

## GFDL-CM4 Absence from ssp126

GFDL-CM4 has no ssp126 source data (no NetCDF files in `data/GFDL-CM4/ssp126/`).
This is not treated as an error. The `code/compute_model_mean.py` script discovers
files by globbing `output/*_ssp126_county_daily_tas.parquet`, so GFDL-CM4 is
naturally absent. The processing log records this fact.

`n_candidate_models` for ssp126 reflects at most 11 models (10 without IPSL).

## Calendar Handling

CMIP6 models use four different calendars, stored as mixed date types in the
per-model Parquet files:

| Calendar | Models | Parquet date type | Notes |
|---|---|---|---|
| `365_day` | FGOALS-g3, INM-CM5-0, INM-CM4-8 | `datetime.date` | No Feb 29 |
| `noleap` | NorESM2-MM, NorESM2-LM, TaiESM1, GFDL-CM4, IPSL-CM6A-LR, CanESM5 | `datetime.date` | No Feb 29 |
| `proleptic_gregorian` | MRI-ESM2-0, MPI-ESM1-2-HR | `datetime.date` | Includes Feb 29 |
| `360_day` | KACE-1-0-G | `string` (YYYY-MM-DD) | 12 × 30-day months |

To handle these differences, all dates are normalized to `VARCHAR` (string) via
`CAST("date" AS VARCHAR)` before grouping. This ensures that Feb 29 exists only
for proleptic_gregorian models, 360_day-only dates (e.g., Feb 30) exist only
for KACE-1-0-G, and standard dates like Jan 15 exist for all models.

## Dynamic Denominator (n_candidate_models)

The cross-model mean is computed as:

```text
tas_mean_k = SUM(available non-missing tas_centroid_grid_k)
             /
             COUNT(available non-missing tas_centroid_grid_k)
```

The denominator is `n_candidate_models`, which varies by county-date because:

- Calendar differences mean not every model has every nominal date
  (e.g., 360_day has no Jan 31; noleap has no Feb 29).
- Some model values may be missing (null).

`n_candidate_models` is computed by first aggregating each model file
to one row per (date, county) — handling any duplicate rows within a model —
then counting the number of contributing models in the outer aggregation.

### Example

```
date        PAC    n_candidate_models
2015-01-15  CountyA  11
2015-02-28  CountyA  11
2015-02-29  CountyA   3    (only proleptic_gregorian models)
2015-02-30  CountyA   1    (only KACE-1-0-G / 360_day)
```

## How to Run

```bash
python code/compute_model_mean.py
```

The script requires DuckDB (`pip install duckdb` or `conda install -c conda-forge duckdb`).

The aggregation reads all per-model Parquet files for each scenario and writes
the 6 output tables to `output/`. DuckDB uses out-of-core execution, so memory
usage stays low even with large input files.

## Dependencies

- **DuckDB** (new): SQL aggregation engine for cross-model mean computation.
- Existing packages: pandas, NumPy, xarray, PyArrow, NetCDF4.
