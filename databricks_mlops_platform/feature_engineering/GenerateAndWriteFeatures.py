# Databricks notebook source
##################################################################################
# Feature Engineering Notebook
#
# Computes credit-risk features from the raw applicant data and writes them to a Unity
# Catalog feature table, plus a scoring-input table used by batch inference.
#
# The transform logic itself lives in feature_engineering/features/credit_features.py so
# it can be unit-tested without a cluster. This notebook is only orchestration: read,
# apply, write.
#
# Tasks of write_feature_table_job
# (resources/feature-engineering-workflow-resource.yml).
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
dbutils.widgets.text("scoring_sample_fraction", "0.2", "Scoring sample fraction")

env = dbutils.widgets.get("env")
scoring_fraction = float(dbutils.widgets.get("scoring_sample_fraction"))

from platform_utils.naming import AssetNames

names = AssetNames(
    catalog=dbutils.widgets.get("catalog_name"),
    schema=dbutils.widgets.get("schema_name"),
)

print(f"env            = {env}")
print(f"feature table  = {names.credit_features}")
print(f"scoring input  = {names.scoring_input}")

# COMMAND ----------

# DBTITLE 1,Load the source data
from sklearn.datasets import fetch_openml
from sklearn.preprocessing import LabelEncoder

# In a real deployment this reads from the gold layer of the medallion ETL. The public
# Credit-G dataset stands in for that source so the pipeline is runnable end-to-end
# without customer data.
data = fetch_openml("credit-g", version=1, as_frame=True, parser="auto")
df = data.frame.copy()
df["class"] = (df["class"] == "bad").astype(int)

for column in df.select_dtypes(include="category").columns:
    df[column] = LabelEncoder().fit_transform(df[column].astype(str))

print(f"Loaded {len(df)} applicant records")

# COMMAND ----------

# DBTITLE 1,Compute features
from pyspark.sql import functions as F

from feature_engineering.features.credit_features import compute_features_fn

raw_sdf = spark.createDataFrame(df).withColumn(
    # Credit-G has no natural key; a stable synthetic id lets predictions be joined back
    # to applicants and to ground-truth outcomes later.
    "customer_id",
    F.concat(F.lit("C"), F.lpad(F.monotonically_increasing_id().cast("string"), 6, "0")),
)

features_sdf = compute_features_fn(raw_sdf).withColumn("computed_at", F.current_timestamp())

# COMMAND ----------

# DBTITLE 1,Write the feature table
(
    features_sdf.write.mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable(names.credit_features)
)
print(f"Wrote {features_sdf.count()} rows to {names.credit_features}")

# COMMAND ----------

# DBTITLE 1,Write the scoring input
# A held-out slice with the label dropped: batch inference must never see the outcome it
# is predicting, or the demo would quietly be scoring on leaked labels.
scoring_sdf = (
    features_sdf.sample(fraction=scoring_fraction, seed=42)
    .drop("class")
    .withColumn("scoring_batch_date", F.current_date())
)

(
    scoring_sdf.write.mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable(names.scoring_input)
)
print(f"Wrote {scoring_sdf.count()} rows to {names.scoring_input}")

# COMMAND ----------

# DBTITLE 1,Write ground-truth outcomes
# Held separately from the scoring input, mirroring reality: outcomes arrive weeks or
# months after the decision. Monitoring joins them in once available to compute realised
# default rates, which is what the challenger-promotion criteria depend on.
ground_truth_sdf = features_sdf.select(
    "customer_id",
    F.col("class").alias("is_default"),
    F.current_timestamp().alias("outcome_recorded_at"),
)

(
    ground_truth_sdf.write.mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable(names.ground_truth)
)
print(f"Wrote {ground_truth_sdf.count()} rows to {names.ground_truth}")

dbutils.notebook.exit("FEATURES_WRITTEN")
