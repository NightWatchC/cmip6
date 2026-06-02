# CMIP6 County-Level Daily Mean Temperature

Processes gridded CMIP6 daily near-surface air temperature (`tas`) NetCDF files
into county-level daily Parquet tables.

## Quick Start

1. Configure `config.py`: set paths, scenarios, optional model filters, and model calendars.
2. Place data under `data/{ModelName}/{scenario}/`.
3. Run the CLI in the Agent conda environment:

```bash
python process_cmip6.py                    # all discovered models and configured scenarios
python process_cmip6.py --model FGOALS-g3  # one model
python process_cmip6.py --model GFDL-CM4 --scenario ssp245
python process_cmip6.py --assemble-only    # assemble existing temp Parquets
```

`--scenario` requires `--model`.

## How It Works

1. Load `data/CountyCoordinate.dta` once when `process_cmip6.py` is imported.
2. Walk `DATA_DIR/{model}/{scenario}/`, skip `historical`, ignore scenarios not listed in `config.py`, and regex-parse matching NetCDF filenames.
3. For each file, load `tas`, match every county centroid to the nearest latitude and longitude grid coordinates, extract daily values, and write a temporary Parquet file.
4. Concatenate temporary Parquets into one output per model-scenario pair.
5. Remove temporary Parquets unless cleanup is disabled.

The processing code is pure Python. ArcPy is not required for the CLI.

## Output

```text
output/{model}_{scenario}_county_daily_tas.parquet
```

| Column | Type | Description |
|---|---|---|
| `date` | `date32` or `string` | `string` is used only for `360_day` calendars |
| `county` | `string` | County name from `CountyCoordinate.dta` |
| `tas_mean_k` | `float32` | Daily near-surface air temperature in Kelvin |

## Configuration

All settings are in `config.py`. Key entries:

```python
# --- Paths ---
DATA_DIR = PROJECT_DIR / "data"
OUTPUT_DIR = PROJECT_DIR / "output"
COUNTY_COORDINATE_FILE = PROJECT_DIR / "data/CountyCoordinate.dta"

# --- Scenarios ---
VALID_SCENARIOS = ("ssp126", "ssp245", "ssp585")

# --- Optional model filter (None = process all discovered) ---
MODEL_FILTER = None  # e.g. {"FGOALS-g3", "GFDL-CM4"}

# --- Model calendars ---
MODEL_CALENDAR = {
    "FGOALS-g3": "365_day",
    "GFDL-CM4": "noleap",
    # ... one entry per model
}
```

If a discovered model has no calendar entry, the script falls back to
`365_day`. Add an explicit entry to `MODEL_CALENDAR` to avoid silently using
the wrong calendar.

## Temporary Files

The CLI uses `TEMP_DIR/parquet_temps`. Useful options:

```bash
python process_cmip6.py --skip-assembly --no-cleanup
python process_cmip6.py --assemble-only
python process_cmip6.py --no-cleanup
```

`--skip-assembly` alone still performs cleanup, so combine it with
`--no-cleanup` when temporary Parquets must be retained. `--assemble-only`
assembles and deletes the temporary Parquet files it consumes, but it does not
run the final empty-directory cleanup step.

## ArcGIS Pro Toolbox

`cmip6_toolbox.pyt` is an optional ArcGIS Pro GUI wrapper around
`process_cmip6.py`. It provides data-directory, model, scenario, and
output-directory parameters, then imports and calls the processing functions.

The toolbox runs processing inside the ArcGIS Pro Python process. That
environment therefore needs working pandas, NumPy, xarray, PyArrow, and NetCDF
backend packages in addition to ArcPy.

The toolbox reassigns `TEMP_PARQUET_DIR` to
`{output_dir}/temp/parquet_temps`, but `process_all_files()` has a default
temporary path captured when `process_cmip6.py` is imported. With the current
code, toolbox processing still writes temp Parquets to the import-time
`TEMP_DIR/parquet_temps` path. Assembly deletes consumed temp files; the toolbox
cleanup call targets the reassigned output-directory path. Also, selecting a
different toolbox data directory does not reload `COUNTY_COORDINATE_FILE`,
which is loaded at module import from `.env`.

Neither `process_cmip6.py` nor `cmip6_toolbox.pyt` creates a `scratch.gdb`.

## Supported Calendars

| Calendar | Models configured in `config.py` |
|---|---|
| `365_day` | FGOALS-g3, INM-CM5-0, INM-CM4-8 |
| `noleap` | NorESM2-MM, NorESM2-LM, TaiESM1, GFDL-CM4, IPSL-CM6A-LR, CanESM5 |
| `proleptic_gregorian` | MRI-ESM2-0, MPI-ESM1-2-HR |
| `360_day` | KACE-1-0-G |

## Adding a New Model

See `FUTURE_WORK.md`. No Python change is required when the model uses a
supported calendar, the expected filename pattern, one-dimensional latitude and
longitude coordinates, and a `tas(time, lat, lon)`-compatible layout.

## Pipeline Reference

`process_cmip6.py` is organized in four phases matching the processing order:

### Phase 1: File Discovery

Functions: `discover_files()`, `log_discovery_summary()`

Walks `DATA_DIR/{model}/{scenario}/`, skips `historical` and unrecognized
scenarios, and regex-parses matching CMIP6 filenames into `FileTask` objects.
Reports discovered and missing model-scenario pairs.

### Phase 2: Process Each NetCDF File

Functions: `generate_dates()`, `_discover_coord_vars()`,
`_match_centroids_to_grid()`, `process_one_file()`, `process_all_files()`

For each file, generates calendar-aware dates from the filename range, opens
the NetCDF with xarray, discovers latitude/longitude coordinate variables via
CF conventions, matches county centroids to their nearest grid cells, extracts
`tas[time, lat_idx, lon_idx]`, builds a long-form Arrow table, and writes a
Snappy-compressed temporary Parquet.

### Phase 3: Assembly

Functions: `assemble_model_scenario()`, `assemble_all()`

Concatenates temporary Parquets into one final output per model-scenario pair
using `pyarrow.parquet.ParquetWriter`. Deletes consumed temporary files.

### Phase 4: Cleanup

Functions: `cleanup_temp_files()`

Removes the temporary Parquet directory and the `TEMP_DIR` when empty.
