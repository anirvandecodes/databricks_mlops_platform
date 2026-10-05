# Databricks notebook source
# Team B — batch scoring with @Champion. The input carries only keys: score_batch reads
# the model's feature metadata and looks up exactly the columns it was trained on from
# Team A's table. That is why a producer's *additive* release (new columns) cannot change
# Team B's predictions, and why deprecated columns keep working until they are removed.

# COMMAND ----------

# MAGIC %pip install -q databricks-feature-engineering pyyaml scikit-learn
# MAGIC %restart_python

# COMMAND ----------

# MAGIC %run ../../shared/feature_contract_utils

# COMMAND ----------

import json

import mlflow
from databricks.feature_engineering import FeatureEngineeringClient
from mlflow.tracking import MlflowClient
from pyspark.sql import functions as F

for w in ("feature_config_path", "catalog", "producer_schema", "consumer_schema"):
    dbutils.widgets.text(w, "")
variables = {k: dbutils.widgets.get(k).strip() for k in ("catalog", "producer_schema", "consumer_schema")}
cfg = load_yaml(dbutils.widgets.get("feature_config_path").strip(), variables)
model_name = cfg["model"]["name"]
predictions_table = f"{variables['catalog']}.{variables['consumer_schema']}.predictions"

mlflow.set_registry_uri("databricks-uc")
champ = MlflowClient().get_model_version_by_alias(model_name, "Champion")
print(f"Scoring with {model_name} v{champ.version} (@Champion)")
print("Features looked up:", json.loads(champ.tags.get("feature_dependencies", "{}")))

# COMMAND ----------

# Score every customer at the latest snapshot that Team A has published.
spine = spark.table(cfg["spine_table"])
latest = spine.agg(F.max("as_of_date")).first()[0]
to_score = spine.where(F.col("as_of_date") == latest).select("customer_id", "as_of_date")

scored = FeatureEngineeringClient().score_batch(model_uri=f"models:/{model_name}@Champion", df=to_score)
result = (scored.select("customer_id", "as_of_date", F.col("prediction").alias("predicted_default"))
                .withColumn("model_version", F.lit(int(champ.version)))
                .withColumn("scored_at", F.current_timestamp()))
result.write.mode("append").option("mergeSchema", "true").saveAsTable(predictions_table)

n = result.count()
print(f"Scored {n:,} customers as of {latest} -> {predictions_table}")
display(spark.sql(f"""
  SELECT model_version, date_trunc('minute', scored_at) AS scored_at, count(*) AS n,
         round(avg(predicted_default), 4) AS predicted_default_rate
  FROM {predictions_table} GROUP BY ALL ORDER BY scored_at"""))

# COMMAND ----------

dbutils.notebook.exit(json.dumps({
    "model_version": int(champ.version), "scored": n, "as_of_date": str(latest),
    "predicted_default_rate": round(result.agg(F.avg("predicted_default")).first()[0], 4),
}))
