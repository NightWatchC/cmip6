# Future Work: Adding More CMIP6 Models

Adding another model normally requires data download and `config.py`
configuration, not Python edits. This applies when the new data matches the
assumptions already implemented in `process_cmip6.py`.

## Quick Start

### 1. Download the data

```text
data/{ModelName}/{scenario}/tas_day_{ModelName}_{scenario}_r1i1p1f1_{grid}_{YYYYMMDD}-{YYYYMMDD}.nc
```

The accepted grid token matches `g[rn]\d*`, such as `gn`, `gr`, or `gr1`.

### 2. Add the model calendar to `config.py`

```python
MODEL_CALENDAR = {
    # ... existing entries ...
    "ModelName": "noleap",
}
```

Use one of the four supported calendars listed below. The script falls back to
`365_day` for an unregistered model, which may produce incorrect dates. Always
add an explicit calendar entry to `config.py`.

### 3. Run a model-specific check

```bash
python process_cmip6.py --model {ModelName}
```

## Implemented Data Assumptions

The current processor is reusable across models when:

- Files use the expected `tas_day_...nc` filename pattern.
- Future scenarios appear under configured `SCENARIOS`; `historical` is skipped.
- The NetCDF variable is named `tas`.
- Latitude and longitude are discoverable through CF `axis` attributes, CF
  `units`, or the supported fallback names.
- Latitude and longitude coordinates are one-dimensional.
- The `tas` array is compatible with indexing as `tas[time, lat, lon]`.
- Nearest latitude and longitude coordinate matching is appropriate for the grid.

The processor does not perform polygon overlay or curvilinear-grid lookup.

## Calendar Reference

| Model | Calendar | Days/year | Rule |
|---|---|---|---|
| KACE-1-0-G | `360_day` | 360 | 12 x 30-day months |
| NorESM2-MM | `noleap` | 365 | No Feb 29 |
| NorESM2-LM | `noleap` | 365 | No Feb 29 |
| INM-CM5-0 | `365_day` | 365 | No Feb 29 |
| INM-CM4-8 | `365_day` | 365 | No Feb 29 |
| TaiESM1 | `noleap` | 365 | No Feb 29 |
| MRI-ESM2-0 | `proleptic_gregorian` | 365/366 | Gregorian leap years |
| MPI-ESM1-2-HR | `proleptic_gregorian` | 365/366 | Gregorian leap years |
| IPSL-CM6A-LR | `noleap` | 365 | No Feb 29 |
| GFDL-CM4 | `noleap` | 365 | No Feb 29 |
| CanESM5 | `noleap` | 365 | No Feb 29 |
| FGOALS-g3 | `365_day` | 365 | No Feb 29 |

## Calendar Types

| Calendar | Days/year | Date generation | Typical filename end | Parquet date storage |
|---|---|---|---|---|
| `365_day` | 365 | Skip Feb 29 | `1231` | `pa.date32()` |
| `noleap` | 365 | Skip Feb 29 | `1231` | `pa.date32()` |
| `proleptic_gregorian` | 365/366 | Gregorian | `1231` | `pa.date32()` |
| `360_day` | 360 | 12 x 30 days | `1230` | `pa.string()` |

For unsupported calendars, extend `generate_dates()` and the Parquet date
storage logic if needed.

## Other Variables

Only `tas` is implemented. Supporting `pr`, `tasmax`, `tasmin`, or `huss`
requires code changes: filename parsing, NetCDF variable selection, output
column naming, and documentation must be updated together.

## Checkpoint for New Models

- [ ] Download data to `data/{ModelName}/{scenario}/`.
- [ ] Confirm filenames match the expected pattern.
- [ ] Confirm `tas` dimensions and coordinate arrays match the implemented assumptions.
- [ ] Add the model to `MODEL_CALENDAR` in `config.py`.
- [ ] Run `python process_cmip6.py --model {ModelName}`.
- [ ] Verify row count: `2856 x total_days`.
- [ ] Check for NULL values and spot-check plausible Kelvin values.

## Known Assemble-Only Limitation

Normal processing keeps model names intact. In `--assemble-only` mode, existing
temporary Parquet filenames are grouped by splitting on `_`, so model names
containing underscores are not grouped correctly without a code change.
