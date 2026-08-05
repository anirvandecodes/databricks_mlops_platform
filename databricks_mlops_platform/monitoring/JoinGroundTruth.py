# Databricks notebook source
##################################################################################
# Ground Truth Join Notebook
#
# Backfills matured outcomes into the inference log's ground_truth column, which batch
# scoring can only write as a null placeholder.
#
# Without this step the data profiling monitor produces its data-quality half (column
# profiles, drift) but none of its model-quality half (accuracy, precision, recall,
# confusion matrix) — those all derive from label_col, and a fully null label column
# yields empty panels rather than an error, which makes the omission easy to miss.
#
# Runs before DriftCheck so a drift investigation reads labels that are as current as
# the outcomes available at that moment.
#
# "JoinGroundTruth" task of monitoring_job (resources/monitoring-resource.yml).
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

# DBTITLE 1,Coverage before the join
from monitoring.label_join import join_ground_truth, label_coverage

if not spark.catalog.tableExists(names.inference_log):
    raise RuntimeError(
        f"{names.inference_log} does not exist. Run the batch inference job at least "
        "once before joining labels."
    )

before = label_coverage(spark, names.inference_log)
print(f"Before: {before['labelled']}/{before['total']} labelled ({before['coverage']:.1%})")

# COMMAND ----------

# DBTITLE 1,Merge matured outcomes into the log
after = join_ground_truth(
    spark,
    inference_log_table=names.inference_log,
    ground_truth_table=names.ground_truth,
)

newly_labelled = after["labelled"] - before["labelled"]
print(f"After : {after['labelled']}/{after['total']} labelled ({after['coverage']:.1%})")
print(f"Newly labelled this run: {newly_labelled}")

if after["unlabelled"]:
    # Expected, not a fault: recent predictions have not had time to mature. Surfaced so
    # the number is visible rather than inferred.
    print(
        f"{after['unlabelled']} rows still awaiting outcomes — model-quality metrics "
        "cover the labelled subset only."
    )

# COMMAND ----------

# DBTITLE 1,Publish coverage for downstream tasks
dbutils.jobs.taskValues.set("labelled_rows", after["labelled"])
dbutils.jobs.taskValues.set("label_coverage", after["coverage"])

dbutils.notebook.exit(f"LABELLED:{after['labelled']}/{after['total']}")
