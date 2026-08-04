# Databricks notebook source
##################################################################################
# Batch Inference Notebook
#
# Scores the current applicant batch with whichever version is @champion, writes the
# predictions, and appends to the inference log that the data profiling monitor watches.
#
# Note what this notebook does NOT do: it never names a model version. It resolves
# models:/<model>@champion at run time, which is why a promotion or a rollback takes
# effect here with no change to this code and no redeployment.
#
# "batch_inference_job" (resources/batch-inference-workflow-resource.yml).
##################################################################################

# COMMAND ----------

# MAGIC %pip install -r ../../requirements.txt

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Notebook arguments
import sys

sys.path.append("../..")

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

# DBTITLE 1,Resolve the champion model
import mlflow

mlflow.set_registry_uri("databricks-uc")

from platform_utils.promotion import CHAMPION, get_alias_version

champion_version = get_alias_version(names.model_name, CHAMPION)
if not champion_version:
    raise RuntimeError(
        f"No @{CHAMPION} alias on {names.model_name}. Run the training workflow and "
        "promote a version before scoring."
    )

model_uri = names.model_uri(CHAMPION)
print(f"Scoring with {model_uri} (currently v{champion_version})")
print(f"Input : {names.scoring_input}")
print(f"Output: {names.raw_predictions}")

# COMMAND ----------

# DBTITLE 1,Score the batch
from deployment.batch_inference.predict import (
    append_inference_log,
    score_batch,
    write_predictions,
)

predictions = score_batch(
    spark,
    model_uri=model_uri,
    input_table=names.scoring_input,
    model_version=champion_version,
)

written = write_predictions(predictions, names.raw_predictions)
print(f"Wrote {written} predictions to {names.raw_predictions}")

# COMMAND ----------

# DBTITLE 1,Append to the inference log
from feature_engineering.features.credit_features import MONITORED_FEATURE_COLUMNS

logged = append_inference_log(
    predictions,
    inference_log_table=names.inference_log,
    monitored_columns=MONITORED_FEATURE_COLUMNS,
)
print(f"Appended {logged} rows to {names.inference_log}")

# COMMAND ----------

# DBTITLE 1,Summarise the scoring run
from pyspark.sql import functions as F

summary = predictions.select(
    F.count("*").alias("rows"),
    F.round(F.mean("prediction"), 4).alias("mean_pd"),
    F.round(F.min("prediction"), 4).alias("min_pd"),
    F.round(F.max("prediction"), 4).alias("max_pd"),
).collect()[0]

print(
    f"Batch summary: {summary['rows']} scored, "
    f"mean PD={summary['mean_pd']}, range [{summary['min_pd']}, {summary['max_pd']}]"
)

dbutils.jobs.taskValues.set("scored_rows", int(summary["rows"]))
dbutils.jobs.taskValues.set("model_version", str(champion_version))

dbutils.notebook.exit(f"SCORED:{summary['rows']}")
