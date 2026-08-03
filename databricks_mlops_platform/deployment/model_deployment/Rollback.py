# Databricks notebook source
##################################################################################
# Rollback Notebook
#
# Re-points @champion at a previously good version. This is the incident procedure, and
# it is deliberately the cheapest operation in the platform: one alias write, seconds to
# execute, no redeploy and no retraining.
#
# Rollback is intentionally NOT gated on approval. Requiring sign-off to stop serving a
# bad model would extend the incident — the gate exists to control what goes live, not to
# obstruct taking something down. The rollback is still fully audited.
#
# Run as: databricks bundle run rollback_job -t <env>
#   with optional --params target_version=<n> to pick a specific version, otherwise the
#   version immediately preceding the current champion is used.
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
dbutils.widgets.text("target_version", "", "Target version (blank = previous)")
dbutils.widgets.text("reason", "manual rollback", "Rollback reason")

env = dbutils.widgets.get("env")
target_version = dbutils.widgets.get("target_version").strip() or None
reason = dbutils.widgets.get("reason")

from platform_utils.naming import AssetNames

names = AssetNames(
    catalog=dbutils.widgets.get("catalog_name"),
    schema=dbutils.widgets.get("schema_name"),
    model=dbutils.widgets.get("model_name"),
)

# COMMAND ----------

# DBTITLE 1,Show current state before changing anything
import mlflow

mlflow.set_registry_uri("databricks-uc")

from platform_utils.promotion import CHAMPION, get_alias_version

before = get_alias_version(names.model_name, CHAMPION)
print(f"Model            : {names.model_name}")
print(f"Current champion : v{before}")
print(f"Requested target : v{target_version if target_version else '(previous version)'}")
print(f"Reason           : {reason}")

# COMMAND ----------

# DBTITLE 1,Roll the alias back
from platform_utils.promotion import rollback_champion

result = rollback_champion(names.model_name, target_version=target_version)
print(result["detail"])

# COMMAND ----------

# DBTITLE 1,Audit the rollback
# A rollback is a production change and is recorded with the same rigour as a promotion —
# an auditor should see both directions of every alias move.
from platform_utils.audit import build_evidence, write_evidence

spark.sql(f"CREATE VOLUME IF NOT EXISTS {names.audit_volume}")

evidence = build_evidence(
    model_name=names.model_name,
    version=str(result["rolled_back_to"]),
    approver=f"rollback-operator:{env}",
    environment=env,
    eval_metrics={"rollback_reason": reason, "rolled_back_from": str(before)},
    decision="ROLLED_BACK",
)
evidence_path = write_evidence(
    spark, evidence, volume_path=names.audit_volume_path, audit_table=names.audit_table
)
print(f"Rollback evidence written: {evidence_path}")

# COMMAND ----------

after = get_alias_version(names.model_name, CHAMPION)
print(f"@champion is now v{after}. Batch and serving jobs pick this up on next resolution.")

dbutils.notebook.exit(f"ROLLED_BACK:{after}")
