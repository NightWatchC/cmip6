# -*- coding: utf-8 -*-
"""
CMIP6 Processing Toolbox for ArcGIS Pro.

Provides a script tool to compute county-level daily mean near-surface
air temperature from CMIP6 NetCDF files. ArcPy is used only for the
GUI parameter layer — all processing is delegated to process_cmip6.py.
"""

import os
import sys
import arcpy

# Ensure the toolbox directory is on sys.path
_TOOLBOX_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLBOX_DIR not in sys.path:
    sys.path.insert(0, _TOOLBOX_DIR)


class Toolbox:
    def __init__(self):
        self.label = "CMIP6 Processing"
        self.alias = "cmip6"
        self.description = (
            "Tools for processing CMIP6 daily near-surface air temperature "
            "data into county-level Parquet tables."
        )
        self.tools = [ProcessCountyDailyTas]


class ProcessCountyDailyTas:
    """Compute county-level daily mean temperature from CMIP6 NetCDF files."""

    def __init__(self):
        self.label = "Process County Daily Temperature"
        self.description = (
            "For each model-scenario combination, extract daily county-level "
            "near-surface air temperature (tas) from CMIP6 NetCDF files using "
            "centroid-based cell matching. Outputs one Parquet table per "
            "model-scenario pair."
        )
        self.canRunInBackground = True
        self.category = "Climate"

    def getParameterInfo(self):
        params = []

        param0 = arcpy.Parameter(
            displayName="Data Directory",
            name="data_dir",
            datatype="DEWorkspace",
            parameterType="Required",
            direction="Input",
        )
        param0.filter.list = ["Folder"]
        params.append(param0)

        param1 = arcpy.Parameter(
            displayName="Model (optional)",
            name="model",
            datatype="GPString",
            parameterType="Optional",
            direction="Input",
        )
        param1.filter.type = "ValueList"
        params.append(param1)

        param2 = arcpy.Parameter(
            displayName="Scenario (optional)",
            name="scenario",
            datatype="GPString",
            parameterType="Optional",
            direction="Input",
        )
        param2.filter.type = "ValueList"
        param2.filter.list = ["ssp126", "ssp245", "ssp585"]
        params.append(param2)

        param3 = arcpy.Parameter(
            displayName="Output Directory",
            name="output_dir",
            datatype="DEWorkspace",
            parameterType="Required",
            direction="Output",
        )
        param3.filter.list = ["Folder"]
        params.append(param3)

        return params

    def updateParameters(self, parameters):
        data_dir = parameters[0].valueAsText
        if not data_dir:
            return

        if not parameters[1].altered:
            import process_cmip6 as pm
            try:
                tasks = pm.discover_files(data_dir)
                models = sorted({t.model for t in tasks})
                parameters[1].filter.list = models
            except Exception:
                pass

        if not parameters[3].altered and data_dir:
            out = os.path.join(os.path.dirname(data_dir), "output")
            parameters[3].value = out

    def updateMessages(self, parameters):
        data_dir = parameters[0].valueAsText
        output_dir = parameters[3].valueAsText

        if data_dir and not os.path.isdir(data_dir):
            parameters[0].setErrorMessage("Data directory does not exist.")

        if output_dir and data_dir:
            if os.path.normpath(output_dir) == os.path.normpath(data_dir):
                parameters[3].setErrorMessage(
                    "Output directory must differ from data directory.")

    def execute(self, parameters, messages):
        import process_cmip6 as pm
        from pathlib import Path

        data_dir = parameters[0].valueAsText
        model_filter = parameters[1].valueAsText or None
        scenario_filter = parameters[2].valueAsText or None
        output_dir = parameters[3].valueAsText

        # Override module-level paths for this run
        pm.DATA_DIR = Path(data_dir)
        pm.OUTPUT_DIR = Path(output_dir)
        pm.TEMP_DIR = pm.OUTPUT_DIR / "temp"
        pm.TEMP_PARQUET_DIR = pm.TEMP_DIR / "parquet_temps"

        pm._setup_file_log(pm.PROJECT_DIR / "process.log")
        pm.logger.info("=== CMIP6 Processing (ArcGIS Pro Tool) ===")

        messages.AddMessage("Discovering NetCDF files...")
        tasks = pm.discover_files(data_dir)

        if model_filter:
            tasks = [t for t in tasks if t.model == model_filter]
            messages.AddMessage(
                f"Filtered to model={model_filter}: {len(tasks)} files")
        if scenario_filter:
            tasks = [t for t in tasks if t.scenario == scenario_filter]
            messages.AddMessage(
                f"Filtered to scenario={scenario_filter}: {len(tasks)} files")

        if not tasks:
            messages.AddWarningMessage("No files to process.")
            return

        pm.log_discovery_summary(tasks)

        messages.AddMessage(
            f"Processing {len(tasks)} files "
            f"across {len({(t.model, t.scenario) for t in tasks})} "
            f"model-scenario pairs...")

        try:
            results = pm.process_all_files(tasks)

            messages.AddMessage("Assembling final Parquet files...")
            output_paths = pm.assemble_all(results, output_dir=pm.OUTPUT_DIR)
            for p in output_paths:
                messages.AddMessage(f"  Output: {p}")

            pm.cleanup_temp_files()
            messages.AddMessage("Temporary files cleaned up.")

            messages.AddMessage("=== Done ===")
            for p in output_paths:
                fsize = os.path.getsize(p) / (1024 ** 3)
                messages.AddMessage(f"  {p} ({fsize:.2f} GB)")

        except Exception:
            pm.logger.exception("Processing failed.")
            raise
