# Databricks notebook source
##################################################################################
# Drift Check Notebook
#
# Computes PSI for each monitored feature against the training baseline and decides
# whether retraining is warranted.
#
# Governance principle: drift triggers model *building*, never model *promotion*. A
# retrained model enters as a challenger and still has to clear validation and the
# approval gate. Automated drift response must not become an unreviewed path to
# production.
#
# "drift_check" task of monitoring_job (resources/monitoring-resource.yml). Sets the task
# value `retrain_required`, which a condition task branches on.
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
dbutils.widgets.text("psi_warn_threshold", "0.10", "PSI warn threshold")
dbutils.widgets.text("psi_retrain_threshold", "0.25", "PSI retrain threshold")
dbutils.widgets.text("num_buckets", "10", "Histogram buckets")

env = dbutils.widgets.get("env")
warn_threshold = float(dbutils.widgets.get("psi_warn_threshold"))
retrain_threshold = float(dbutils.widgets.get("psi_retrain_threshold"))
num_buckets = int(dbutils.widgets.get("num_buckets"))

from platform_utils.naming import AssetNames

names = AssetNames(
    catalog=dbutils.widgets.get("catalog_name"),
    schema=dbutils.widgets.get("schema_name"),
    model=dbutils.widgets.get("model_name"),
)

print(f"PSI thresholds: warn >= {warn_threshold}, retrain >= {retrain_threshold}")

# COMMAND ----------

# DBTITLE 1,Bucket both distributions on shared edges
from feature_engineering.features.credit_features import MONITORED_FEATURE_COLUMNS

baseline = spark.table(names.baseline_table)
live = spark.table(names.inference_log)

live_count = live.count()
print(f"baseline rows={baseline.count()}, live rows={live_count}")
if live_count == 0:
    dbutils.jobs.taskValues.set("retrain_required", "false")
    dbutils.notebook.exit("NO_LIVE_DATA")


def bucket_counts(df, column, edges):
    """Count rows per bucket using edges derived from the baseline.

    Both distributions must be bucketed on the *same* edges, otherwise the PSI comparison
    is meaningless — this is the most common way a hand-rolled PSI goes wrong.
    """
    from pyspark.sql import functions as F

    bucket_expr = F.lit(0)
    for index, edge in enumerate(edges):
        bucket_expr = F.when(F.col(column) > edge, F.lit(index + 1)).otherwise(bucket_expr)

    counts = (
        df.filter(F.col(column).isNotNull())
        .withColumn("_bucket", bucket_expr)
        .groupBy("_bucket")
        .count()
        .collect()
    )
    by_bucket = {row["_bucket"]: row["count"] for row in counts}
    return [float(by_bucket.get(i, 0)) for i in range(len(edges) + 1)]


# COMMAND ----------

# DBTITLE 1,Compute PSI per monitored feature
from platform_utils.metrics import population_stability_index, psi_verdict

results = []
for column in MONITORED_FEATURE_COLUMNS:
    if column not in baseline.columns or column not in live.columns:
        print(f"  skip {column}: not present in both tables")
        continue

    # Quantile edges from the baseline give roughly equal-mass buckets, which keeps PSI
    # stable for skewed features like credit_amount.
    probabilities = [i / num_buckets for i in range(1, num_buckets)]
    edges = baseline.approxQuantile(column, probabilities, 0.01)
    edges = sorted(set(edges))
    if not edges:
        print(f"  skip {column}: baseline has no variation")
        continue

    baseline_counts = bucket_counts(baseline, column, edges)
    live_counts = bucket_counts(live, column, edges)
    psi = population_stability_index(live_counts, baseline_counts)
    verdict = psi_verdict(psi, warn=warn_threshold, retrain=retrain_threshold)
    results.append((column, psi, verdict))
    print(f"  {column:28s} PSI={psi:.4f}  {verdict}")

# COMMAND ----------

# DBTITLE 1,Persist the drift results
from datetime import datetime, timezone

from pyspark.sql import functions as F

if results:
    drift_df = spark.createDataFrame(
        [(c, float(p), v) for c, p, v in results],
        "feature string, psi double, verdict string",
    ).withColumn("checked_at", F.lit(datetime.now(timezone.utc).isoformat()))

    (
        drift_df.write.format("delta")
        .mode("append")
        .option("mergeSchema", "true")
        .saveAsTable(names.table("drift_check_results"))
    )
    print(f"Appended {len(results)} drift results to {names.table('drift_check_results')}")

# COMMAND ----------

# DBTITLE 1,Decide whether to retrain
breaches = [(c, p) for c, p, v in results if v == "RETRAIN"]
warnings = [(c, p) for c, p, v in results if v == "WARN"]

retrain_required = bool(breaches)
worst = max(results, key=lambda r: r[1]) if results else None

print("=" * 66)
print(f"Features checked : {len(results)}")
print(f"Warnings         : {len(warnings)} {[c for c, _ in warnings]}")
print(f"Retrain breaches : {len(breaches)} {[c for c, _ in breaches]}")
if worst:
    print(f"Worst drift      : {worst[0]} (PSI={worst[1]:.4f})")
print(f"Decision         : {'RETRAIN' if retrain_required else 'NO ACTION'}")
print("=" * 66)

if retrain_required:
    print(
        "\nTriggering retraining. The retrained model will register as a CHALLENGER and "
        "must still pass validation and the approval gate before it can serve traffic."
    )

# Condition task in the workflow branches on this string value.
dbutils.jobs.taskValues.set("retrain_required", "true" if retrain_required else "false")
dbutils.jobs.taskValues.set(
    "drift_reason",
    "; ".join(f"{c} PSI={p:.4f}" for c, p in breaches) if breaches else "no breach",
)

dbutils.notebook.exit("RETRAIN" if retrain_required else "STABLE")
