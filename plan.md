# CMIP6 County-Level Daily Temperature Processing

## Overview

The processor extracts daily `tas` from the CMIP6 grid cell that contains each
county centroid. It does not compute county area-weighted averages and does not
assign counties by nearest grid-center distance.

Only configured future scenarios are processed. With current `code/config.py`, those
are `ssp126`, `ssp245`, and `ssp585`; `historical` is skipped.

## Method

1. Load county centroids from `data/CountyCoordinate.dta`.
2. Discover NetCDF files with the `tas_day_{model}_{scenario}_...nc` pattern.
3. Open each dataset and discover latitude and longitude coordinate variables.
4. Validate that both coordinate variables are one-dimensional, numeric, finite,
   and strictly monotonic.
5. Reject 2D, curvilinear, non-monotonic, or incompatible coordinates with an
   error containing model, scenario, file path, coordinate names, and shapes.
6. Infer grid-cell bounds from adjacent coordinate-center midpoints. First and
   last outer bounds are extrapolated by half the adjacent spacing.
7. Normalize county centroid longitudes to match the grid longitude convention.
8. Locate each centroid in the latitude and longitude bound intervals.
9. Raise a clear error if any centroid is outside the inferred grid bounds.
10. Extract `tas[:, lat_idx, lon_idx]` and write long-form Parquet output.

Boundary intervals are deterministic: lower bound inclusive and upper bound
exclusive, except the final interval includes its upper bound.

## Output Design

Final temperature table:

```text
output/{model}_{scenario}_county_daily_tas.parquet
```

| Column | Type | Description |
|---|---|---|
| `date` | `pa.date32()` or `pa.string()` | `360_day` uses strings |
| `county` | `pa.string()` | County name |
| `tas_centroid_grid_k` | `pa.float32()` | Daily `tas` from the centroid-containing grid cell |

Audit table:

```text
output/grid_audit/{model}_{scenario}_county_grid_audit.parquet
```

Audit columns include county name, original centroid longitude/latitude,
normalized centroid longitude, grid latitude/longitude indices, selected grid
centers, selected grid bounds, model, scenario, and grid label.

## Important Implementation Notes

- Coordinate discovery still uses CF `axis`, CF `units`, then fallback names:
  `lat`, `latitude`, `nav_lat`, `lon`, `longitude`, `nav_lon`.
- The data array is still assumed compatible with `tas[time, lat, lon]`.
- Per-file failures are logged and the batch continues, matching the previous
  failure-handling behavior.
- `--skip-assembly --no-cleanup` keeps temporary data and audit Parquets.
- `--assemble-only` assembles existing data temp Parquets. It relies on existing
  audit temp Parquets if audit assembly is expected.
- Existing temp grouping in `--assemble-only` splits filenames on `_`; model
  names containing underscores still require a code fix.

## Calendar Handling

Calendar logic is unchanged:

| Calendar | Rule | Parquet date storage |
|---|---|---|
| `365_day` | Skip Feb 29 | `pa.date32()` |
| `noleap` | Skip Feb 29 | `pa.date32()` |
| `proleptic_gregorian` | Gregorian leap days | `pa.date32()` |
| `360_day` | 12 x 30-day months | `pa.string()` |

Models missing from `MODEL_CALENDAR` still fall back to `365_day`; add explicit
entries in `code/config.py` for reliable dates.

## ArcGIS Pro Toolbox

`cmip6_toolbox.pyt` is an optional GUI wrapper. It imports and runs
`code/process_cmip6.py` in the ArcGIS Pro Python process and therefore needs working
processing packages there. Neither the CLI nor the toolbox creates a
`scratch.gdb`.

## Verification Checklist

1. Compile `code/process_cmip6.py` and `code/config.py`.
2. Confirm no `argmin()` nearest-center matching remains in processing logic.
3. Verify invalid 2D or non-monotonic coordinates raise clear errors.
4. Verify 1D increasing and decreasing coordinates assign points by containment.
5. Confirm final schema: `date`, `county`, `tas_centroid_grid_k`.
6. Inspect `output/grid_audit/` for county-to-grid mapping outputs.
