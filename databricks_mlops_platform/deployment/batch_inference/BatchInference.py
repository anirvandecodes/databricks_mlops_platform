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
# A/B experiment controls. Off by default so nothing changes unless an experiment is
# explicitly enabled; when on, a deterministic slice of customers is scored by the
# challenger instead of the champion. See platform_utils/variants.py.
dbutils.widgets.dropdown("ab_enabled", "false", ["true", "false"], "A/B experiment enabled")
dbutils.widgets.text("ab_variant_b_alias", "challenger", "A/B arm-B alias")
dbutils.widgets.text("ab_traffic_split_pct", "10", "A/B traffic to arm B (%)")
dbutils.widgets.text("ab_split_salt", "credit_risk_ab_v1", "A/B cohort salt")
# Whether starting a live experiment requires an approved challenger (true in prod).
dbutils.widgets.dropdown("approval_required", "false", ["true", "false"], "Approval required")

env = dbutils.widgets.get("env")
ab_enabled = dbutils.widgets.get("ab_enabled").strip().lower() == "true"
ab_variant_b_alias = dbutils.widgets.get("ab_variant_b_alias")
ab_split_pct = int(dbutils.widgets.get("ab_traffic_split_pct"))
ab_split_salt = dbutils.widgets.get("ab_split_salt")
approval_required = dbutils.widgets.get("approval_required").strip().lower() == "true"

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

# An A/B run needs a resolvable arm-B alias in addition to the champion. If A/B is off, or
# the arm-B alias is unset, the notebook falls back to the champion-only path unchanged.
variant_b_version = get_alias_version(names.model_name, ab_variant_b_alias) if ab_enabled else None
run_ab = ab_enabled and bool(variant_b_version)

print(f"Champion   : v{champion_version} ({names.model_uri(CHAMPION)})")
if ab_enabled and not variant_b_version:
    print(
        f"A/B requested but no @{ab_variant_b_alias} alias on {names.model_name}; "
        "scoring champion-only."
    )
print(f"A/B mode   : {'ON' if run_ab else 'OFF'}"
      + (f" — arm B @{ab_variant_b_alias} v{variant_b_version} at {ab_split_pct}%" if run_ab else ""))
print(f"Input      : {names.scoring_input}")
print(f"Output     : {names.raw_predictions}")

# COMMAND ----------

# DBTITLE 1,Gate a live experiment, then score
from deployment.batch_inference.predict import (
    append_inference_log,
    score_batch,
    score_batch_ab,
    write_predictions,
)
from platform_utils.variants import ARM_CHAMPION, ARM_CHALLENGER

if run_ab:
    # Starting a live split serves the challenger to real customers, so it is gated like a
    # promotion (in prod). A blocked experiment fails the job rather than silently scoring
    # champion-only — an attempted unapproved live split must be loud.
    from platform_utils.experiment import (
        DECISION_EXPERIMENT_STARTED,
        assert_experiment_allowed,
        experiment_metrics,
    )

    detail = assert_experiment_allowed(
        model_name=names.model_name,
        b_version=variant_b_version,
        environment=env,
        approval_required=approval_required,
    )
    print(f"Experiment gate: {detail}")

    # Record that a live experiment ran, on what basis — the same immutable evidence trail
    # promotions use.
    from platform_utils.audit import build_evidence, write_evidence

    spark.sql(f"CREATE VOLUME IF NOT EXISTS {names.audit_volume}")
    evidence = build_evidence(
        model_name=names.model_name,
        version=str(variant_b_version),
        approver=f"experiment-operator:{env}",
        environment=env,
        eval_metrics=experiment_metrics(
            split_pct=ab_split_pct,
            salt=ab_split_salt,
            arm_b_alias=ab_variant_b_alias,
            arm_b_version=variant_b_version,
            champion_version=champion_version,
        ),
        decision=DECISION_EXPERIMENT_STARTED,
    )
    write_evidence(spark, evidence, volume_path=names.audit_volume_path, audit_table=names.audit_table)

    predictions = score_batch_ab(
        spark,
        input_table=names.scoring_input,
        arms=[
            (ARM_CHAMPION, names.model_uri(CHAMPION), champion_version),
            (ARM_CHALLENGER, names.model_uri(ab_variant_b_alias), variant_b_version),
        ],
        split_pct=ab_split_pct,
        salt=ab_split_salt,
    )
else:
    predictions = score_batch(
        spark,
        model_uri=names.model_uri(CHAMPION),
        input_table=names.scoring_input,
        model_version=champion_version,
    )

written = write_predictions(predictions, names.raw_predictions)
print(f"Wrote {written} predictions to {names.raw_predictions}")

# COMMAND ----------

# DBTITLE 1,Append to the inference log
from feature_engineering.features.credit_features import MONITORED_FEATURE_COLUMNS

# The same decision threshold validation gates on. Imported rather than restated so the
# class the monitor scores is the class the model was approved on.
#
# Fully qualified: this notebook runs from deployment/batch_inference with the repo root on
# sys.path, so a bare `validation` binds the *directory* as a namespace package and the name
# is not found. ModelValidation gets away with the short form only because it runs with
# validation/ as its working directory.
from validation.validation import DECISION_THRESHOLD

logged = append_inference_log(
    predictions,
    inference_log_table=names.inference_log,
    monitored_columns=MONITORED_FEATURE_COLUMNS,
    decision_threshold=DECISION_THRESHOLD,
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

# In A/B mode, print the split that was actually served so the realised percentage can be
# checked against the configured one, and record per-arm counts as task values.
if run_ab and "variant" in predictions.columns:
    per_arm = {
        r["variant"]: r["n"]
        for r in predictions.groupBy("variant").agg(F.count("*").alias("n")).collect()
    }
    print(f"A/B split served: {per_arm}")
    dbutils.jobs.taskValues.set("ab_arm_counts", per_arm)

dbutils.jobs.taskValues.set("scored_rows", int(summary["rows"]))
dbutils.jobs.taskValues.set("model_version", str(champion_version))

dbutils.notebook.exit(f"SCORED:{summary['rows']}")
