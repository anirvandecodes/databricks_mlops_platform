# Databricks notebook source
##################################################################################
# Model Deployment Notebook
#
# Promotes the approved candidate to @champion and records the audit evidence.
#
# "Deploy" is a metadata operation: re-pointing an alias. Nothing is redeployed, no
# endpoint is rebuilt, no pipeline is rerun. Batch and serving jobs resolve
# models:/<model>@champion at run time, so they pick up the new version on their next
# execution — and the same mechanism run backwards is the rollback.
#
# Evidence is written *before* the alias moves, so an approval can never be recorded
# without its supporting basis.
#
# "ModelDeployment" task of model_training_job (resources/model-workflow-resource.yml).
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
dbutils.widgets.dropdown("approval_required", "false", ["true", "false"], "Approval required")
dbutils.widgets.text("git_source_info", "", "Git source info")

env = dbutils.widgets.get("env")
approval_required = dbutils.widgets.get("approval_required").strip().lower() == "true"
git_source_info = dbutils.widgets.get("git_source_info")

from platform_utils.naming import AssetNames

names = AssetNames(
    catalog=dbutils.widgets.get("catalog_name"),
    schema=dbutils.widgets.get("schema_name"),
    model=dbutils.widgets.get("model_name"),
)

from platform_utils.task_values import resolve_candidate_version, task_value

model_version = resolve_candidate_version(dbutils, names)

approver = task_value(dbutils, "ApprovalGate", "approver", f"service-principal:{env}")
run_id = task_value(dbutils, "Train", "run_id", "")
training_data_version = task_value(dbutils, "Train", "training_data_version", "")
validation_metrics = task_value(dbutils, "ModelValidation", "validation_metrics", {})

print(f"Promoting {names.model_name} v{model_version} in '{env}' (approver: {approver})")

# COMMAND ----------

# DBTITLE 1,Ensure the audit volume exists
import mlflow

mlflow.set_registry_uri("databricks-uc")

# Created idempotently rather than assumed: a missing volume must not be the reason a
# promotion loses its audit record.
spark.sql(f"CREATE VOLUME IF NOT EXISTS {names.audit_volume}")
print(f"Audit volume ready: {names.audit_volume_path}")

# COMMAND ----------

# DBTITLE 1,Write the promotion evidence
from platform_utils.audit import build_evidence, write_evidence

evidence = build_evidence(
    model_name=names.model_name,
    version=str(model_version),
    approver=approver,
    environment=env,
    eval_metrics=validation_metrics or {},
    training_data_version=training_data_version,
    git_commit=git_source_info,
    run_id=run_id,
    decision="APPROVED_AND_PROMOTED",
)

evidence_path = write_evidence(
    spark,
    evidence,
    volume_path=names.audit_volume_path,
    audit_table=names.audit_table,
)
print(f"Evidence written: {evidence_path}")

# COMMAND ----------

# DBTITLE 1,Move the champion alias
from platform_utils.promotion import promote_to_champion

# approval_required is passed through so this notebook enforces the gate too, even if it
# is ever run outside the workflow that already checked it.
result = promote_to_champion(
    model_name=names.model_name,
    version=str(model_version),
    environment=env,
    approval_required=approval_required,
)

print(f"Promotion: {result['detail']}")
if result["changed"]:
    print(f"  @champion: v{result['previous_champion']} -> v{result['version']}")
    print(f"  Rollback if needed: databricks bundle run rollback_job -t {env}")
else:
    print("  No alias change was required.")

# COMMAND ----------

# DBTITLE 1,Emit task values
dbutils.jobs.taskValues.set("promoted_version", str(model_version))
dbutils.jobs.taskValues.set("previous_champion", str(result.get("previous_champion") or ""))
dbutils.jobs.taskValues.set("evidence_path", evidence_path)

dbutils.notebook.exit(f"PROMOTED:{model_version}")
