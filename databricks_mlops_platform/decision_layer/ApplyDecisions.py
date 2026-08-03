# Databricks notebook source
##################################################################################
# Decision Layer Notebook
#
# Turns model scores into business decisions: registers the policy rules as Unity Catalog
# functions, then applies them to the current batch of predictions.
#
# Runs as a task separate from scoring on purpose. Policy changes far more often than
# models, and this separation means a band adjustment or a limit cap deploys without
# retraining, re-scoring, or a model risk review.
#
# "ApplyDecisionRules" task of batch_inference_job
# (resources/batch-inference-workflow-resource.yml).
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

# DBTITLE 1,Register the policy functions
from decision_layer.policy import credit_limit_function_ddl, eligibility_function_ddl

# CREATE OR REPLACE, so the deployed policy always matches what is in version control.
spark.sql(eligibility_function_ddl(names.fn_evaluate_eligibility))
spark.sql(credit_limit_function_ddl(names.fn_calculate_credit_limit))
print(f"Registered {names.fn_evaluate_eligibility}")
print(f"Registered {names.fn_calculate_credit_limit}")

# COMMAND ----------

# DBTITLE 1,Apply the policy to the scored batch
from pyspark.sql import functions as F

predictions = spark.table(names.raw_predictions)

# Credit-G carries no KYC field; derive a stand-in so the hard-filter path is exercised.
# In a real deployment this joins the customer master record instead.
enriched = predictions.withColumn(
    "kyc_status",
    F.when(F.col("age") >= 18, F.lit("VERIFIED")).otherwise(F.lit("PENDING")),
)

decisions = (
    enriched.withColumn(
        "policy_eval",
        F.expr(f"{names.fn_evaluate_eligibility}(prediction, age, kyc_status)"),
    )
    .withColumn("is_eligible", F.col("policy_eval.eligible"))
    .withColumn("risk_band", F.col("policy_eval.risk_band"))
    .withColumn("rejection_reason", F.col("policy_eval.rejection_reason"))
    .withColumn(
        "offered_credit_limit",
        F.expr(f"{names.fn_calculate_credit_limit}(policy_eval.risk_band, credit_amount)"),
    )
    .withColumn("decided_at", F.current_timestamp())
    .drop("policy_eval")
)

# COMMAND ----------

# DBTITLE 1,Write the decisions
(
    decisions.write.format("delta")
    .mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable(names.credit_decisions)
)
print(f"Wrote {decisions.count()} decisions to {names.credit_decisions}")

# COMMAND ----------

# DBTITLE 1,Summarise the decision distribution
# Printed per run because a sudden shift in the band mix is often the first visible sign of
# a model or data problem — ahead of any drift metric refreshing.
summary = (
    decisions.groupBy("risk_band")
    .agg(
        F.count("*").alias("applicants"),
        F.round(F.avg("prediction"), 4).alias("avg_pd"),
        F.round(F.avg("offered_credit_limit"), 0).alias("avg_limit"),
    )
    .orderBy("risk_band")
)
summary.show(truncate=False)

approval_rate = decisions.filter(F.col("is_eligible")).count() / max(decisions.count(), 1)
print(f"Overall approval rate: {approval_rate:.2%}")

rejections = (
    decisions.filter(~F.col("is_eligible")).groupBy("rejection_reason").count().orderBy("count", ascending=False)
)
print("Rejection reasons:")
rejections.show(truncate=False)

dbutils.jobs.taskValues.set("approval_rate", float(approval_rate))
dbutils.notebook.exit(f"DECISIONS:{decisions.count()}")
