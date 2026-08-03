# Databricks notebook source
##################################################################################
# Approval Gate Notebook (Stage 2 — Model Gate)
#
# Stage 1 is the code gate: branch protection, peer review, unit tests and bundle
# validation on the pull request. That gates *code*.
#
# This is Stage 2, the model gate. Code passing review says nothing about whether a
# particular trained artifact should carry production traffic, so promotion is gated
# separately on a reviewer's assessment of this version's metrics.
#
# The gate is a hard technical control, not a convention: in prod (approval_required=true)
# an unapproved version raises and the job fails. The pipeline cannot promote a model that
# no one signed off.
#
# How a reviewer approves: set the tag approval_status=approved on the model version
# (Models UI > version > Tags, or the API). Optionally set approved_by. Using a UC tag
# means the approval is itself a governed, lineage-tracked object.
#
# "ApprovalGate" task of model_training_job (resources/model-workflow-resource.yml).
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

env = dbutils.widgets.get("env")
approval_required = dbutils.widgets.get("approval_required").strip().lower() == "true"

from platform_utils.naming import AssetNames

names = AssetNames(
    catalog=dbutils.widgets.get("catalog_name"),
    schema=dbutils.widgets.get("schema_name"),
    model=dbutils.widgets.get("model_name"),
)

from platform_utils.task_values import resolve_candidate_version

# Falls back to the challenger alias when this task is re-run on its own, which is exactly
# what an operator does after signing off on a previously blocked promotion.
model_version = resolve_candidate_version(dbutils, names)

print(f"environment       = {env}")
print(f"model             = {names.model_name}")
print(f"candidate version = {model_version}")
print(f"approval required = {approval_required}")

# COMMAND ----------

# DBTITLE 1,Surface the evidence a reviewer needs
import mlflow
from mlflow import MlflowClient

mlflow.set_registry_uri("databricks-uc")
client = MlflowClient()

version_detail = client.get_model_version(names.model_name, str(model_version))
tags = version_detail.tags or {}

from platform_utils.promotion import CHAMPION, get_alias_version

current_champion = get_alias_version(names.model_name, CHAMPION)

print("--- Promotion request ---------------------------------------------")
print(f"Candidate       : v{model_version}")
print(f"Current champion: v{current_champion if current_champion else '(none — first deployment)'}")
print(f"Validation      : {tags.get('validation_status', 'NOT RUN')}")
print(f"Run ID          : {version_detail.run_id}")
for key in sorted(k for k in tags if k.startswith("validation_") and k != "validation_status"):
    print(f"  {key.replace('validation_', '')}: {tags[key]}")
print("-------------------------------------------------------------------")

# A candidate that failed the automated bar must never be presented as approvable, even
# if someone has already tagged it — defence in depth against an out-of-order approval.
if tags.get("validation_status") == "FAILED":
    raise RuntimeError(
        f"Version {model_version} failed model validation and is not eligible for "
        "promotion. Retrain or fix the model before requesting approval."
    )

# COMMAND ----------

# DBTITLE 1,Enforce the gate
from platform_utils.promotion import APPROVAL_TAG, APPROVED_VALUE, is_approved

approved, detail = is_approved(names.model_name, model_version)
print(f"Approval check: {detail}")

if approval_required and not approved:
    # The gate holds. The job fails here, the challenger stays a challenger, and the
    # champion alias is untouched, so production continues serving the last approved model.
    raise RuntimeError(
        f"\n{'=' * 70}\n"
        f"PROMOTION BLOCKED — approval required in environment '{env}'.\n"
        f"{'=' * 70}\n"
        f"Model  : {names.model_name}\n"
        f"Version: {model_version}\n\n"
        f"{detail}\n\n"
        f"To approve, set this tag on the model version and re-run this task:\n"
        f"    {APPROVAL_TAG} = {APPROVED_VALUE}\n"
        f"    approved_by    = <reviewer identity>\n\n"
        f"Production keeps serving champion v{current_champion} until then.\n"
        f"{'=' * 70}"
    )

decision = "APPROVED" if approved else "AUTO_APPROVED"
print(f"Gate passed ({decision}). Proceeding to deployment.")

dbutils.jobs.taskValues.set("approval_decision", decision)
dbutils.jobs.taskValues.set("approver", tags.get("approved_by", f"service-principal:{env}"))
dbutils.jobs.taskValues.set("model_version", str(model_version))

dbutils.notebook.exit(decision)
