# Databricks notebook source
##################################################################################
# Monitor Setup Notebook
#
# Idempotently attaches a data profiling monitor with an Inference profile to the inference
# log table. (Data profiling was formerly called Lakehouse Monitoring; the SDK namespace is
# still w.quality_monitors.)
#
# The monitor is what produces the profile and drift metric tables plus the generated
# dashboard, and its drift table computes population_stability_index natively — so the
# platform does not register a PSI SQL function of its own.
#
# Run once per environment, and again after the monitored schema changes.
#
# "setup_monitor" task of monitoring_job (resources/monitoring-resource.yml).
##################################################################################

# COMMAND ----------

# MAGIC %pip install -r ../requirements.txt

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Notebook arguments
import sys

sys.path.append("..")

dbutils.widgets.dropdown("env", "dev", ["dev", "staging", "prod"], "Environment")
dbutils.widgets.text("catalog_name", "workspace", "UC catalog")
dbutils.widgets.text("schema_name", "mlops_dev", "UC schema")
dbutils.widgets.text("model_name", "credit_risk_model", "Model name")

env = dbutils.widgets.get("env")

from platform_utils.naming import AssetNames

names = AssetNames(
    catalog=dbutils.widgets.get("catalog_name"),
    schema=dbutils.widgets.get("schema_name"),
    model=dbutils.widgets.get("model_name"),
)

# COMMAND ----------

# DBTITLE 1,Ensure the inference log exists
# The monitor cannot attach to a table that does not exist yet, which is the usual reason
# monitoring setup fails on a first deployment.
if not spark.catalog.tableExists(names.inference_log):
    raise RuntimeError(
        f"{names.inference_log} does not exist. Run the batch inference job at least once "
        "before setting up the monitor."
    )

row_count = spark.table(names.inference_log).count()
print(f"{names.inference_log} has {row_count} rows")

# COMMAND ----------

# DBTITLE 1,Create or refresh the data profiling monitor
from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import DatabricksError, ResourceDoesNotExist
from databricks.sdk.service.catalog import MonitorInferenceLog, MonitorInferenceLogProblemType

w = WorkspaceClient()

monitor_config = dict(
    output_schema_name=names.fq_schema,
    inference_log=MonitorInferenceLog(
        problem_type=MonitorInferenceLogProblemType.PROBLEM_TYPE_CLASSIFICATION,
        prediction_col="prediction",
        label_col="ground_truth",
        timestamp_col="scored_at",
        model_id_col="model_version",
        # Daily granularity matches the batch scoring cadence. A finer window would
        # produce buckets too sparse for a stable PSI reading.
        granularities=["1 day"],
    ),
    baseline_table_name=names.baseline_table,
)

# Data profiling is available on the tiers this platform targets, including Free Edition —
# verified by creating an active monitor there. The fallback below exists only for tiers or
# regions where the API is genuinely absent, which is a capability gap rather than a
# pipeline fault: the governance chain (train, validate, gate, promote, score) does not
# depend on the managed monitor, and DriftCheck computes PSI in Python against the baseline
# table rather than reading the monitor's metric tables.
#
# Matched on the endpoint-absent response only. Permission and configuration errors still
# raise, so this cannot mask a real misconfiguration as an unsupported feature.
def _monitoring_unsupported(err: DatabricksError) -> bool:
    return "no api found" in str(err).lower()


monitor_ready = True

try:
    # Create-or-update on the typed exception, not on message text: the SDK signals "no
    # monitor yet" with ResourceDoesNotExist, whose message mentions a monitor ID rather
    # than any "not found" wording, so string matching silently misses the first-run case.
    try:
        existing = w.quality_monitors.get(table_name=names.inference_log)
    except ResourceDoesNotExist:
        existing = None

    if existing is None:
        # assets_dir is accepted only on create — update() rejects it with a TypeError,
        # which is why it is passed here rather than in the shared config.
        w.quality_monitors.create(
            table_name=names.inference_log,
            assets_dir=f"/Workspace/Shared/monitoring/{names.schema}/{names.model}",
            **monitor_config,
        )
        print(f"Created monitor on {names.inference_log}")
    else:
        print(f"Monitor already exists (status: {existing.status}); updating configuration.")
        w.quality_monitors.update(table_name=names.inference_log, **monitor_config)
except DatabricksError as err:
    if not _monitoring_unsupported(err):
        raise
    monitor_ready = False
    print(
        "Data profiling (data quality monitoring) is unavailable in this workspace "
        f"({w.config.host}); skipping monitor attachment.\n"
        f"  API response: {err}\n"
        "  Drift detection still runs: DriftCheck computes PSI in Python against "
        f"{names.baseline_table}, and the retraining branch is unaffected. Only the "
        "managed profile/drift metric tables and the generated dashboard are absent."
    )

# COMMAND ----------

# DBTITLE 1,Report the generated assets
if monitor_ready:
    monitor = w.quality_monitors.get(table_name=names.inference_log)

    print("Monitor will populate:")
    print(f"  profile metrics: {names.profile_metrics}")
    print(f"  drift metrics  : {names.drift_metrics}")
    print("\nThese appear after the monitor's first refresh, which may take several minutes.")

    # The generated dashboard is the artefact worth showing an audience, so print a
    # deep link rather than leaving them to find it under the table's Quality tab.
    if monitor.dashboard_id:
        print(f"\nGenerated dashboard: {w.config.host}/sql/dashboardsv3/{monitor.dashboard_id}")

    # Drift rows have preconditions that are easy to mistake for a broken monitor:
    #   BASELINE    drift needs baseline_table_name, set above.
    #   CONSECUTIVE drift needs two populated windows at the configured granularity, so it
    #               stays empty until the log spans a second day.
    days = spark.sql(
        f"SELECT count(DISTINCT date(scored_at)) AS days FROM {names.inference_log}"
    ).collect()[0]["days"]
    if days < 2:
        print(
            f"\nNote: {names.inference_log} spans {days} day(s) at '1 day' granularity, so "
            "CONSECUTIVE drift rows cannot be computed yet — a window needs a prior window "
            "to compare against. BASELINE drift is unaffected."
        )
else:
    print("No managed metric tables or dashboard will be produced in this workspace.")

dbutils.notebook.exit("MONITOR_READY" if monitor_ready else "MONITOR_UNSUPPORTED")
