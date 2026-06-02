"""
CMIP6 County-Level Daily Mean Temperature — Configuration.

This is the single source of configuration. To change paths, scenarios,
model filters, or calendars, edit this file. The .env file is no longer read.
"""

from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Paths (relative to this config file, or absolute)
# ---------------------------------------------------------------------------
DATA_DIR = PROJECT_DIR / "data"
OUTPUT_DIR = PROJECT_DIR / "output"
TEMP_DIR = PROJECT_DIR / "temp"
TEMP_PARQUET_DIR = TEMP_DIR / "parquet_temps"
COUNTY_COORDINATE_FILE = PROJECT_DIR / "data/CountyCoordinate.dta"

# ---------------------------------------------------------------------------
# Scenarios to process
# ---------------------------------------------------------------------------
VALID_SCENARIOS = ("ssp126", "ssp245", "ssp585")

# ---------------------------------------------------------------------------
# Optional: limit to specific models (set to None to process all discovered)
# Example: MODEL_FILTER = {"FGOALS-g3", "GFDL-CM4"}
# ---------------------------------------------------------------------------
MODEL_FILTER = None

# ---------------------------------------------------------------------------
# Model calendar registry — all 12 CMIP6 models
# ---------------------------------------------------------------------------
# Supported calendars: 365_day, noleap, proleptic_gregorian, 360_day
MODEL_CALENDAR = {
    "FGOALS-g3": "365_day",
    "GFDL-CM4": "noleap",
    "KACE-1-0-G": "360_day",
    "NorESM2-MM": "noleap",
    "NorESM2-LM": "noleap",
    "INM-CM5-0": "365_day",
    "INM-CM4-8": "365_day",
    "TaiESM1": "noleap",
    "MRI-ESM2-0": "proleptic_gregorian",
    "MPI-ESM1-2-HR": "proleptic_gregorian",
    "IPSL-CM6A-LR": "noleap",
    "CanESM5": "noleap",
}
