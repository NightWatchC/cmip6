# Requirements

## Recommended Processing Environment

Run `process_cmip6.py` with:

```text
C:\Users\NightWatch\.conda\envs\Agent\python.exe
```

The project Bash hook at `.claude/hooks/python-guard.py` rewrites bare `python`
and `pip` references in inspected Bash commands to this environment.

## Python Packages

The processing script directly imports or uses:

| Package | Purpose |
|---|---|
| pandas | Read `CountyCoordinate.dta` |
| numpy | Coordinate matching and array extraction |
| xarray | Open NetCDF datasets |
| pyarrow | Build and write Parquet tables |
| netCDF4 | Recommended xarray NetCDF backend on Windows |

The current Agent environment also includes scipy, h5py, and h5netcdf for
compatible scientific I/O support.

## ArcGIS Pro Toolbox

`cmip6_toolbox.pyt` is optional. It imports ArcPy to define the ArcGIS Pro user
interface, then imports and calls `process_cmip6.py` in the same ArcGIS Pro
Python process.

Running the toolbox therefore requires ArcPy and working processing packages in
the ArcGIS Pro environment. The current `Python_for_ArcGISpro` environment has
known NetCDF4 DLL issues, so use the Agent environment CLI for reliable
processing unless the ArcGIS Pro environment is repaired.
