# Databricks notebook source
##################################################################################
# Monitor Setup Notebook
#
# Idempotently creates two things:
#   1. The PSI metric as a Unity Catalog SQL function. Registering it in UC — rather than
#      importing a wheel per job — means every team computes drift with the identical,
#      audited formula, and the definition is version-controlled in platform_utils.metrics.
#   2. A data profiling monitor with an Inference profile attached to the inference log
#      table. (Data profiling was formerly called Lakehouse Monitoring; the SDK namespace
#      is still w.quality_monitors.)
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

# DBTITLE 1,Register PSI as a Unity Catalog function
from platform_utils.metrics import psi_function_ddl

spark.sql(psi_function_ddl(names.fn_calculate_psi))
print(f"Registered UC function {names.fn_calculate_psi}")

# Verified against a known-divergent pair rather than assumed: a silently broken metric
# would make the whole drift story untrustworthy.
check = spark.sql(
    f"SELECT {names.fn_calculate_psi}(array(400D,50D,25D,25D), array(100D,100D,100D,100D)) AS psi"
).collect()[0]["psi"]
print(f"  sanity check PSI (heavily shifted distributions) = {check:.4f}")
assert check > 0.25, f"PSI function returned {check}, expected a large drift signal"

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
# depend on the managed monitor, and DriftCheck computes PSI from the UC function registered
# above rather than from the monitor's metric tables.
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
        "  Drift detection still runs: DriftCheck computes PSI from "
        f"{names.fn_calculate_psi} against the baseline table, and the retraining "
        "branch is unaffected. Only the managed profile/drift metric tables are absent."
    )

# COMMAND ----------

# DBTITLE 1,Report the generated metric tables
if monitor_ready:
    print("Monitor will populate:")
    print(f"  profile metrics: {names.profile_metrics}")
    print(f"  drift metrics  : {names.drift_metrics}")
    print("\nThese appear after the monitor's first refresh, which may take several minutes.")
else:
    print("No managed metric tables will be produced in this workspace.")

dbutils.notebook.exit("MONITOR_READY" if monitor_ready else "MONITOR_UNSUPPORTED")
