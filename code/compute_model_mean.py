"""
Compute cross-model mean daily temperature for each CMIP6 scenario.

For each scenario (ssp126, ssp245, ssp585), produces two versions:
  1. Including IPSL-CM6A-LR
  2. Excluding IPSL-CM6A-LR

The mean is computed across all available non-missing model values for each
county-date observation. n_candidate_models records the dynamic denominator.

Supports both the original centroid-based pipeline (default) and the new
overlap-weighted pipeline (--overlap flag).

Usage:
    python code/compute_model_mean.py                     # centroid pipeline
    python code/compute_model_mean.py --overlap           # overlap pipeline
"""
import argparse
import logging
import sys
from pathlib import Path
from typing import Optional

import duckdb

from config import (
    OUTPUT_DIR,
    OVERLAP_FINAL_DIR,
    VALID_SCENARIOS,
)

MODEL_TO_EXCLUDE = "IPSL-CM6A-LR"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def get_model_files(
    scenario: str,
    input_dir: Path,
    pattern: str,
) -> list[Path]:
    """Discover available model Parquet files for a scenario."""
    files = sorted(input_dir.glob(pattern.format(scenario=scenario)))
    if not files:
        logger.warning("No model files found for scenario %s in %s",
                       scenario, input_dir)
    return files


def compute_scenario_mean(
    scenario: str,
    exclude_ipsl: bool,
    input_dir: Path,
    file_pattern: str,
    temp_column: str,
    county_column: str,
    file_suffix_strip: str,
    output_dir: Path,
    output_county_column: str = "NAME",
) -> Optional[str]:
    """Aggregate across models for one scenario and write the output Parquet."""
    files = get_model_files(scenario, input_dir, file_pattern)

    if exclude_ipsl:
        files = [f for f in files if MODEL_TO_EXCLUDE not in f.name]
        version_label = f"without_{MODEL_TO_EXCLUDE}"
    else:
        version_label = f"with_{MODEL_TO_EXCLUDE}"

    model_names = [
        f.stem.replace(file_suffix_strip.format(scenario=scenario), "")
        for f in files
    ]
    logger.info(
        "%s %s: %d models: %s",
        scenario,
        version_label,
        len(model_names),
        model_names,
    )

    if not files:
        logger.warning("No files to process for %s %s", scenario, version_label)
        return None

    out_path = output_dir / f"{scenario}_model_mean_{version_label}.parquet"

    # Build per-file subqueries that each reduce to one row per (date, county).
    # Inner GROUP BY handles duplicate (date, county) rows that may exist in
    # some model files, and normalises date to VARCHAR for mixed calendar types.
    subqueries = []
    for f in files:
        subqueries.append(
            f"SELECT CAST(date AS VARCHAR) AS date, "
            f"\"{county_column}\" AS county, "
            f"AVG(\"{temp_column}\") AS model_tas "
            f"FROM read_parquet('{f.as_posix()}') "
            f"WHERE \"{temp_column}\" IS NOT NULL "
            f"GROUP BY CAST(date AS VARCHAR), \"{county_column}\""
        )
    union_sql = " UNION ALL ".join(subqueries)

    con = duckdb.connect()
    con.execute(
        f"""
        COPY (
            SELECT
                date,
                county AS "{output_county_column}",
                AVG(model_tas)::FLOAT8 AS tas_mean_k,
                COUNT(*)::BIGINT AS n_candidate_models
            FROM ({union_sql})
            GROUP BY date, county
            ORDER BY date, "{output_county_column}"
        ) TO '{out_path.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """
    )
    con.close()

    logger.info("Wrote %s", out_path)
    return str(out_path)


def main(passed_args: Optional[list] = None) -> int:
    if passed_args is None:
        passed_args = sys.argv[1:]

    parser = argparse.ArgumentParser(
        description="Cross-model mean daily temperature aggregation")
    parser.add_argument(
        "--overlap", action="store_true",
        help="Use overlap-weighted pipeline outputs (output_overlap/final/) "
             "instead of centroid-based outputs (output/).")
    args = parser.parse_args(passed_args)

    if args.overlap:
        input_dir = OVERLAP_FINAL_DIR
        file_pattern = "*_{scenario}_county_daily_tas_overlap.parquet"
        temp_column = "tas_mean_k"
        county_column = "PAC"
        output_county_column = "PAC"
        file_suffix_strip = "_{scenario}_county_daily_tas_overlap"
        output_dir = OVERLAP_FINAL_DIR
        logger.info("Using overlap-weighted pipeline outputs from %s", input_dir)
    else:
        input_dir = OUTPUT_DIR
        file_pattern = "*_{scenario}_county_daily_tas.parquet"
        temp_column = "tas_centroid_grid_k"
        county_column = "county"
        output_county_column = "NAME"
        file_suffix_strip = "_{scenario}_county_daily_tas"
        output_dir = OUTPUT_DIR
        logger.info("Using centroid-based pipeline outputs from %s", input_dir)

    for scenario in VALID_SCENARIOS:
        compute_scenario_mean(
            scenario, exclude_ipsl=False,
            input_dir=input_dir, file_pattern=file_pattern,
            temp_column=temp_column, county_column=county_column,
            file_suffix_strip=file_suffix_strip, output_dir=output_dir,
            output_county_column=output_county_column,
        )
        compute_scenario_mean(
            scenario, exclude_ipsl=True,
            input_dir=input_dir, file_pattern=file_pattern,
            temp_column=temp_column, county_column=county_column,
            file_suffix_strip=file_suffix_strip, output_dir=output_dir,
            output_county_column=output_county_column,
        )

    # Log GFDL-CM4 absence from ssp126
    ssp126_files = get_model_files("ssp126", input_dir, file_pattern)
    has_gfdl = any("GFDL-CM4" in f.name for f in ssp126_files)
    if not has_gfdl:
        logger.info(
            "GFDL-CM4 is absent from ssp126 (no source data); "
            "n_candidate_models reflects only available models."
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
