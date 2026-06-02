# CMIP6 County-Level Daily Mean Temperature Processing

## Overview

Process gridded CMIP6 daily near-surface air temperature (`tas`) NetCDF files
into county-level daily Parquet tables. For each model-scenario combination,
extract the daily value at the nearest latitude and longitude grid coordinates
for every county centroid from `data/CountyCoordinate.dta`.

Only configured future projection scenarios are processed. With the current
`config.py`, those are `ssp126`, `ssp245`, and `ssp585`. `historical`
directories are explicitly skipped.

## Environment

| Item | Value |
|---|---|
| Recommended CLI interpreter | `C:\Users\NightWatch\.conda\envs\Agent\python.exe` |
| Core processing packages | pandas, NumPy, xarray, PyArrow, NetCDF4 |
| Additional scientific I/O packages in Agent env | scipy, h5py, h5netcdf |
| ArcGIS Pro toolbox | Optional GUI wrapper; imports and runs the processor inside the ArcGIS Pro Python process |

The current ArcGIS Pro environment has known NetCDF4 DLL issues. Use the Agent
environment CLI for reliable processing unless that environment is repaired.

## Current Downloaded Data

```text
data/
|-- CountyCoordinate.dta
|-- FGOALS-g3/
|   |-- historical/  (165 files, skipped)
|   |-- ssp126/      (86 files)
|   |-- ssp245/      (86 files)
|   `-- ssp585/      (86 files)
`-- GFDL-CM4/
    |-- historical/  (9 files, skipped)
    |-- ssp245/      (5 files)
    `-- ssp585/      (5 files)
```

`GFDL-CM4/ssp126` is absent because those projection files are not present in
the workspace.

## Configured Model Calendars

| Model | Calendar | Grid noted in source data | Typical chunking |
|---|---|---|---|
| KACE-1-0-G | `360_day` | gr | One file per scenario |
| NorESM2-MM | `noleap` | gn | About 10-year blocks |
| NorESM2-LM | `noleap` | gn | About 10-year blocks |
| INM-CM5-0 | `365_day` | gr1 | Multi-decade blocks |
| INM-CM4-8 | `365_day` | gr1 | Multi-decade blocks |
| TaiESM1 | `noleap` | gn | About 10-year blocks |
| MRI-ESM2-0 | `proleptic_gregorian` | gn | Multi-decade blocks |
| MPI-ESM1-2-HR | `proleptic_gregorian` | gn | About 5-year blocks |
| IPSL-CM6A-LR | `noleap` | gr | One file per scenario |
| GFDL-CM4 | `noleap` | gr1/gr2 | About 20-year blocks |
| CanESM5 | `noleap` | gn | One file per scenario |
| FGOALS-g3 | `365_day` | gn | One-year blocks |

Only FGOALS-g3 and GFDL-CM4 data are currently downloaded in this workspace.
The other calendar entries prepare `config.py` for future downloads.

## Input Centroids

- File: `data/CountyCoordinate.dta`
- County count: 2,856
- Identifier: `county`
- Coordinates: `centlat`, `centlon` in WGS84
- Loading: once, at `process_cmip6.py` module import

No boundary shapefile or ArcPy centroid computation is used by the processor.

## Filename Discovery

`discover_files()` walks `DATA_DIR/{model}/{scenario}/`, skips `historical`,
ignores scenarios outside `SCENARIOS`, and accepts filenames matching:

```python
r"^tas_day_(?P<model>.+)_(?P<scenario>ssp\d+)_r1i1p1f1_(?P<grid>g[rn]\d*)_(?P<start>\d{8})-(?P<end>\d{8})\.nc$"
```

The parsed filename supplies the model, scenario, grid token, and inclusive
date range. A `MODEL_FILTER` entry in `config.py` optionally limits discovery.

## Processing Pipeline

### Phase 1: Load Centroids

At module import:

```python
df = pd.read_stata("data/CountyCoordinate.dta")
_CENTROID_NAMES = df["county"].tolist()
_CENTROID_LONS = df["centlon"].values.astype(np.float64)
_CENTROID_LATS = df["centlat"].values.astype(np.float64)
```

### Phase 2: Process Each NetCDF File

1. Generate dates from the filename range and configured model calendar.
2. Open the dataset with `decode_times=False` to read the NetCDF `time` size.
3. Warn when the NetCDF time size differs from the generated date count. If the
   NetCDF has fewer time steps, truncate generated dates to that size. If it has
   more time steps, generated dates are not extended; the later Arrow-table
   construction will normally fail and the per-file error handler will log and
   skip that file.
4. Reopen the dataset, read `tas` as `float32`, and discover latitude and
   longitude coordinate names.
5. Normalize negative centroid longitudes to 0-360 when the grid uses that
   convention.
6. Match each centroid independently to the nearest latitude coordinate and
   nearest longitude coordinate.
7. Extract `tas[:, lat_idx, lon_idx]`, transpose to county-by-day shape, build a
   long-form Arrow table, and write a Snappy-compressed temporary Parquet file.

Coordinate discovery checks CF `axis` attributes, then CF `units`, then these
fallback names:

```text
latitude:  lat, latitude, nav_lat
longitude: lon, longitude, nav_lon
```

The current implementation assumes one-dimensional latitude and longitude
coordinates and a `tas` array compatible with `tas[time, lat, lon]`. It does not
implement polygon overlay or curvilinear-grid matching.

### Phase 3: Assemble Outputs

`assemble_all()` groups successfully processed temporary files by
`(model, scenario)` and concatenates them with `pyarrow.parquet.ParquetWriter`.
After assembly, it deletes the temporary Parquet files it consumed.

### Phase 4: Cleanup

`cleanup_temp_files()` removes `TEMP_PARQUET_DIR` and removes `TEMP_DIR` when it
is empty.

## Calendar-Aware Dates

| Calendar | Days/year | Rule | Parquet storage |
|---|---|---|---|
| `365_day` | 365 | Skip Feb 29 | `pa.date32()` |
| `noleap` | 365 | Skip Feb 29 | `pa.date32()` |
| `proleptic_gregorian` | 365/366 | Include Gregorian leap days | `pa.date32()` |
| `360_day` | 360 | 12 x 30-day months | `pa.string()` in `YYYY-MM-DD` form |

If a model is missing from `MODEL_CALENDAR`, processing currently falls back to
`365_day`. Add an explicit entry to `config.py` for reliable dates.

## Output Design

```text
output/
|-- FGOALS-g3_ssp126_county_daily_tas.parquet
|-- FGOALS-g3_ssp245_county_daily_tas.parquet
`-- ...
```

| Column | Type | Description |
|---|---|---|
| `date` | `pa.date32()` or `pa.string()` | Calendar date |
| `county` | `pa.string()` | County name |
| `tas_mean_k` | `pa.float32()` | Daily temperature in Kelvin |

Model and scenario are encoded in the output filename, not repeated as columns.
Expected row count is `2856 x total_days`.

## Error Handling

- Missing or empty scenario directories produce no tasks; processing continues.
- Filenames that do not match the regex are logged and skipped.
- A failure in one NetCDF file is logged; remaining files continue processing.
- Assembly skips model-scenario groups with no successful temporary files.
- Partial output is possible when one or more files fail.

## CLI Behavior

```bash
python process_cmip6.py
python process_cmip6.py --model FGOALS-g3
python process_cmip6.py --model GFDL-CM4 --scenario ssp245
python process_cmip6.py --skip-assembly --no-cleanup
python process_cmip6.py --assemble-only
python process_cmip6.py --no-cleanup
```

- `--scenario` requires `--model`.
- `--skip-assembly` alone still runs cleanup.
- `--assemble-only` assembles and deletes consumed temporary Parquets, then
  returns without the final empty-directory cleanup.
- Existing temp grouping in `--assemble-only` mode splits filenames on `_`;
  model names containing underscores require a code fix.

## ArcGIS Pro Toolbox Behavior

`cmip6_toolbox.pyt` exposes one ArcGIS Pro tool: **Process County Daily
Temperature**.

The toolbox:

1. Accepts data directory, optional model, optional scenario, and output directory.
2. Populates the model dropdown by calling `discover_files()`.
3. Uses a fixed scenario dropdown containing `ssp126`, `ssp245`, and `ssp585`.
4. Reassigns `DATA_DIR`, `OUTPUT_DIR`, `TEMP_DIR`, and `TEMP_PARQUET_DIR` for the
   run.
5. Imports and calls `process_all_files()`, `assemble_all()`, and
   `cleanup_temp_files()` in the ArcGIS Pro Python process.
6. Reports progress and output paths through ArcGIS geoprocessing messages.

The toolbox does not launch the Agent conda interpreter. Neither the toolbox nor
the CLI creates a `scratch.gdb`.

Two toolbox path limitations follow from the current implementation:

- `process_all_files()` has an import-time default for its temp path. The
  toolbox reassigns `TEMP_PARQUET_DIR` but does not pass that new path into
  `process_all_files()`, so temp Parquets are still written under the
  import-time `TEMP_DIR/parquet_temps`. Assembly deletes consumed temp files,
  while the cleanup call targets the reassigned output-directory temp path.
- `COUNTY_COORDINATE_FILE` and the centroid arrays are loaded at module import.
  Selecting another toolbox data directory does not select or reload another
  centroid file.

## Verification Checklist

1. Confirm output schema: `date`, `county`, `tas_mean_k`.
2. Confirm row count: `2856 x total_days`.
3. Confirm no unexpected NULL values.
4. Spot-check plausible Kelvin values.
5. Confirm `360_day` dates use strings and end each year on `12-30`.
6. Review `process.log` for skipped files, time-size warnings, or per-file failures.

## Future Extension

See `FUTURE_WORK.md` for supported assumptions and the checklist for adding new
models.
