# Requirements

## Python Packages

The current Agent environment uses:

```text
Python 3.13.13
```

The processing and aggregation scripts directly import or use:

| Package | Version | Purpose |
|---|---:|---|
| pandas | 3.0.2 | Read county data and weight tables |
| numpy | 2.3.1 | Array operations |
| xarray | 2026.4.0 | Open NetCDF datasets |
| pyarrow | 24.0.0 | Build and write Parquet tables |
| netCDF4 | 1.7.4 | Recommended xarray NetCDF backend on Windows |
| duckdb | 1.5.3 | Cross-model mean aggregation |
| scipy | 1.17.1 | Sparse matrix operations for weighted averaging |
| geopandas | 1.1.3 | County and grid cell polygon handling |
| shapely | 2.1.2 | Polygon intersection and geometry operations |
| pyproj | 3.7.2 | Geodesic area calculation (WGS84 ellipsoid) |
| rtree | 1.4.1 | Spatial indexing for efficient polygon intersection |

The current Agent environment also includes:

| Package | Version | Purpose |
|---|---:|---|
| h5py | 3.16.0 | HDF5 support |
| h5netcdf | 1.8.1 | Alternative NetCDF backend support |

## ArcGIS Pro Toolbox

`cmip6_toolbox.pyt` is optional. It imports ArcPy to define the ArcGIS Pro user
interface, then imports and calls `code/process_cmip6.py` in the same ArcGIS Pro
Python process.

Running the toolbox therefore requires ArcPy and working processing packages in
the ArcGIS Pro environment. The current `Python_for_ArcGISpro` environment has
known NetCDF4 DLL issues, so use the Agent environment CLI for reliable
processing unless the ArcGIS Pro environment is repaired.
