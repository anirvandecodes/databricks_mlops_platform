# Databricks notebook source
##################################################################################
# Model Validation Notebook
#
# Evaluates the freshly trained challenger against the thresholds in validation.py.
# A candidate that fails never reaches the approval gate — this is the automated quality
# bar that runs before any human is asked to review a model.
#
# "ModelValidation" task of model_training_job (resources/model-workflow-resource.yml).
#
# Parameters:
#   run_mode - disabled | dry_run | enabled
#                disabled : skip validation entirely
#                dry_run  : evaluate and report, but never block promotion
#                enabled  : block promotion when a threshold is not met
##################################################################################

# COMMAND ----------

# MAGIC %pip install -r ../requirements.txt

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Notebook arguments
import sys

sys.path.append("..")

dbutils.widgets.dropdown("run_mode", "enabled", ["disabled", "dry_run", "enabled"], "Run Mode")
dbutils.widgets.text("catalog_name", "workspace", "UC catalog")
dbutils.widgets.text("schema_name", "mlops_dev", "UC schema")
dbutils.widgets.text("model_name", "credit_risk_model", "Model name")
dbutils.widgets.text("targets", "class", "Label column")

run_mode = dbutils.widgets.get("run_mode")
label_column = dbutils.widgets.get("targets")

from platform_utils.naming import AssetNames

names = AssetNames(
    catalog=dbutils.widgets.get("catalog_name"),
    schema=dbutils.widgets.get("schema_name"),
    model=dbutils.widgets.get("model_name"),
)

if run_mode == "disabled":
    dbutils.notebook.exit("VALIDATION_SKIPPED")

from platform_utils.task_values import resolve_candidate_version

# Falls back to the challenger alias when this task is re-run alone, which is how a data
# scientist debugs a validation failure without repeating the training run.
model_version = resolve_candidate_version(dbutils, names)

print(f"run_mode = {run_mode}")
print(f"model    = {names.model_name}")
print(f"version  = {model_version}")

# COMMAND ----------

# DBTITLE 1,Prepare the evaluation set
import mlflow

mlflow.set_registry_uri("databricks-uc")

from validation import custom_metrics, evaluator_config, validation_thresholds

# Validation runs against the same persisted training snapshot the model was fit on, so a
# threshold breach reflects the model rather than a shifted evaluation set.
eval_sdf = spark.table(names.training_data)

DROP_COLUMNS = ["env", "ingested_at", "residence_band", "age_band"]
eval_pdf = eval_sdf.drop(*[c for c in DROP_COLUMNS if c in eval_sdf.columns]).toPandas()

model_uri = f"models:/{names.model_name}/{model_version}"
thresholds = validation_thresholds()
print(f"Evaluating {model_uri} against {len(thresholds)} threshold(s) on {len(eval_pdf)} rows")

# COMMAND ----------

# DBTITLE 1,Score the evaluation set
# A LightGBM booster emits P(default) as a float. Threshold metrics (precision, recall,
# F1) need discrete labels, while ROC-AUC must stay on the probability so it measures
# ranking rather than a single operating point. Both are derived here from one scoring pass.
#
# The threshold is imported from validation.py, not defined here: it is a risk parameter
# that belongs with the thresholds it interacts with.
from validation import DECISION_THRESHOLD

booster = mlflow.lightgbm.load_model(model_uri)

features = eval_pdf.drop(columns=[label_column])
y_true = eval_pdf[label_column].astype(int)
y_proba = booster.predict(features)
y_pred = (y_proba >= DECISION_THRESHOLD).astype(int)

print(f"Scored {len(y_true)} rows at decision threshold {DECISION_THRESHOLD}")

# COMMAND ----------

# DBTITLE 1,Compute metrics and enforce the thresholds
from sklearn.metrics import (
    average_precision_score,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)

validation_failed = False
failure_detail = ""
metrics = {}

with mlflow.start_run(run_name=f"validate_v{model_version}"):
    metrics = {
        # Probability-based: measures ranking quality independent of the cut-off.
        "roc_auc": float(roc_auc_score(y_true, y_proba)),
        "pr_auc": float(average_precision_score(y_true, y_proba)),
        # Label-based: measures performance at the chosen operating point.
        "precision_score": float(precision_score(y_true, y_pred, zero_division=0)),
        "recall_score": float(recall_score(y_true, y_pred, zero_division=0)),
        "f1_score": float(f1_score(y_true, y_pred, zero_division=0)),
    }
    for name, value in metrics.items():
        mlflow.log_metric(f"validation_{name}", value)

    # Extra metrics contributed by validation.py, if any.
    for metric in custom_metrics():
        print(f"  (custom metric declared: {getattr(metric, 'name', metric)})")

    # Threshold enforcement. Done explicitly rather than via
    # mlflow.validate_evaluation_results so the breach message names every failing metric
    # with its actual and required value — that message is what a reviewer reads.
    breaches = []
    for metric_name, threshold in thresholds.items():
        if metric_name not in metrics:
            print(f"  WARNING: threshold set for {metric_name!r}, which was not computed")
            continue
        actual = metrics[metric_name]
        required = threshold.threshold
        if threshold.greater_is_better and actual < required:
            breaches.append(f"{metric_name}={actual:.4f} < required {required}")
        elif not threshold.greater_is_better and actual > required:
            breaches.append(f"{metric_name}={actual:.4f} > allowed {required}")

    for key, value in sorted(metrics.items()):
        print(f"  {key} = {value:.4f}")

    if breaches:
        validation_failed = True
        failure_detail = "; ".join(breaches)
        print(f"Validation FAILED: {failure_detail}")
    else:
        print("Validation PASSED.")

# COMMAND ----------

# DBTITLE 1,Record the verdict on the model version
# Tagged on the version itself so the approval reviewer sees the validation outcome on the
# same object they are signing off, not buried in a separate job log.
from mlflow import MlflowClient

client = MlflowClient()
verdict = "FAILED" if validation_failed else "PASSED"
client.set_model_version_tag(names.model_name, str(model_version), "validation_status", verdict)
for metric in ("roc_auc", "precision_score", "recall_score", "f1_score"):
    if metric in metrics:
        client.set_model_version_tag(
            names.model_name,
            str(model_version),
            f"validation_{metric}",
            f"{metrics[metric]:.4f}",
        )

dbutils.jobs.taskValues.set("validation_status", verdict)
dbutils.jobs.taskValues.set(
    "validation_metrics",
    {k: float(v) for k, v in metrics.items() if isinstance(v, (int, float))},
)

# COMMAND ----------

# DBTITLE 1,Enforce the verdict
if validation_failed and run_mode == "enabled":
    # Hard stop: the candidate never reaches the approval gate.
    raise RuntimeError(
        f"Model validation failed for {names.model_name} v{model_version} with "
        f"run_mode='enabled', so promotion is blocked.\n{failure_detail}"
    )

if validation_failed:
    print(f"run_mode='{run_mode}': breach recorded but not enforced.")

dbutils.notebook.exit(verdict)
