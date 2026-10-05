# Databricks notebook source
##################################################################################
# A/B Comparison Notebook
#
# Compares the two arms of a live A/B experiment and writes a per-arm results row-set. It
# only reports — it never promotes. A human reads the comparison and decides; that decision
# then flows through the ordinary approval gate.
#
# Per arm it computes: sample size, approval rate (from credit_decisions), positive-
# prediction rate, PSI of the prediction against the training baseline, and — once outcomes
# have matured into ground_truth — accuracy / precision / recall. A two-proportion z-test on
# the primary metric says whether the arms differ by more than noise.
#
# Reads are filtered by the `variant` column stamped during scoring. When only the champion
# has run (no variant column, or one arm), it reports a single arm and skips the test.
#
# "ABComparison" task of monitoring_job (resources/monitoring-resource.yml).
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
dbutils.widgets.text("num_buckets", "10", "PSI histogram buckets")

env = dbutils.widgets.get("env")
num_buckets = int(dbutils.widgets.get("num_buckets"))

from platform_utils.naming import AssetNames

names = AssetNames(
    catalog=dbutils.widgets.get("catalog_name"),
    schema=dbutils.widgets.get("schema_name"),
    model=dbutils.widgets.get("model_name"),
)

# COMMAND ----------

# DBTITLE 1,Guard: is there an experiment to compare?
from pyspark.sql import functions as F

log = spark.table(names.inference_log)

if "variant" not in log.columns:
    print("No `variant` column in the inference log — no A/B experiment has run. Nothing to compare.")
    dbutils.notebook.exit("NO_EXPERIMENT")

arms = [r["variant"] for r in log.select("variant").distinct().collect() if r["variant"]]
print(f"Arms present in the inference log: {sorted(arms)}")
if len(arms) < 2:
    print("Fewer than two arms present; a comparison needs both. Reporting what exists.")

# COMMAND ----------

# DBTITLE 1,Per-arm quality and rate metrics
# Approval rate comes from the decisions table (business outcome); the rest from the log.
decisions = spark.table(names.credit_decisions) if spark.catalog.tableExists(names.credit_decisions) else None

def _arm_metrics(arm: str) -> dict:
    """Compute one arm's metrics from the inference log (+ decisions for approval rate)."""
    arm_log = log.filter(F.col("variant") == arm)
    n = arm_log.count()

    # model_version this arm was served by (stamped per row; one value per arm).
    versions = [r["model_version"] for r in arm_log.select("model_version").distinct().collect()]
    model_version = versions[0] if len(versions) == 1 else ",".join(map(str, versions))

    positive_pred_rate = arm_log.select(F.avg("prediction")).collect()[0][0]

    # Quality metrics only over the labelled subset (ground_truth matured).
    labelled = arm_log.filter(F.col("ground_truth").isNotNull())
    n_labelled = labelled.count()
    accuracy = precision = recall = None
    correct = None
    if n_labelled:
        conf = labelled.select(
            F.sum(((F.col("prediction") == 1) & (F.col("ground_truth") == 1)).cast("int")).alias("tp"),
            F.sum(((F.col("prediction") == 1) & (F.col("ground_truth") == 0)).cast("int")).alias("fp"),
            F.sum(((F.col("prediction") == 0) & (F.col("ground_truth") == 1)).cast("int")).alias("fn"),
            F.sum(((F.col("prediction") == 0) & (F.col("ground_truth") == 0)).cast("int")).alias("tn"),
        ).collect()[0]
        tp, fp, fn, tn = conf["tp"], conf["fp"], conf["fn"], conf["tn"]
        correct = tp + tn
        accuracy = correct / n_labelled
        precision = tp / (tp + fp) if (tp + fp) else None
        recall = tp / (tp + fn) if (tp + fn) else None

    approval_rate = None
    if decisions is not None and "variant" in decisions.columns:
        arm_dec = decisions.filter(F.col("variant") == arm)
        n_dec = arm_dec.count()
        if n_dec:
            approval_rate = arm_dec.select(F.avg(F.col("is_eligible").cast("double"))).collect()[0][0]

    return {
        "variant": arm,
        "model_version": model_version,
        "n": n,
        "n_labelled": n_labelled,
        "positive_pred_rate": positive_pred_rate,
        "approval_rate": approval_rate,
        "accuracy": accuracy,
        "precision": precision,
        "recall": recall,
        "correct": correct,
    }


metrics = {arm: _arm_metrics(arm) for arm in sorted(arms)}
for arm, m in metrics.items():
    print(f"Arm {arm}: n={m['n']} v{m['model_version']} "
          f"approval={m['approval_rate']} accuracy={m['accuracy']} "
          f"precision={m['precision']} recall={m['recall']}")

# COMMAND ----------

# DBTITLE 1,Score-distribution PSI: challenger vs champion
# The A/B question PSI answers here is "how differently does the challenger score the same
# population?" — so arm A (champion) is the reference distribution and each arm's
# prediction_score is compared against it. Arm A vs itself is ~0 by construction; a large
# arm-B value means the challenger is redistributing risk, which is what a reviewer wants to
# see before trusting the challenger's approval-rate difference. Reuses the platform's PSI
# and the same baseline-edges bucketing DriftCheck uses.
from platform_utils.metrics import population_stability_index
from platform_utils.variants import ARM_CHAMPION

psi_col = "prediction_score" if "prediction_score" in log.columns else "prediction"


def _bucket_counts(df, column, edges):
    bucket = sum((F.col(column) > edge).cast("int") for edge in edges)
    rows = df.filter(F.col(column).isNotNull()).groupBy(bucket.alias("b")).count().collect()
    by_bucket = {r["b"]: r["count"] for r in rows}
    return [float(by_bucket.get(i, 0)) for i in range(len(edges) + 1)]


psi_by_arm = {arm: None for arm in metrics}
reference_arm = ARM_CHAMPION if ARM_CHAMPION in metrics else sorted(metrics)[0]
reference = log.filter(F.col("variant") == reference_arm)
if reference.filter(F.col(psi_col).isNotNull()).limit(1).count():
    # Edges from the reference (champion) score distribution give roughly equal-mass buckets.
    probs = [i / num_buckets for i in range(1, num_buckets)]
    edges = sorted(set(reference.approxQuantile(psi_col, probs, 0.01)))
    if edges:
        ref_counts = _bucket_counts(reference, psi_col, edges)
        for arm in metrics:
            arm_counts = _bucket_counts(log.filter(F.col("variant") == arm), psi_col, edges)
            psi_by_arm[arm] = population_stability_index(arm_counts, ref_counts)
            print(f"Arm {arm}: PSI({psi_col} vs arm {reference_arm}) = {psi_by_arm[arm]:.4f}")

# COMMAND ----------

# DBTITLE 1,Significance on the primary metric
# Primary metric: correct-prediction rate once labels exist (that is what the experiment is
# ultimately about); otherwise approval rate as an early read.
from platform_utils.metrics import two_proportion_z_test

z_result = {"z": None, "p_value": None, "primary_metric": None}
if len(metrics) == 2:
    a, b = sorted(metrics)  # "A" (champion) then "B" (challenger)
    ma, mb = metrics[a], metrics[b]
    if ma["n_labelled"] and mb["n_labelled"]:
        z_result = two_proportion_z_test(ma["correct"], ma["n_labelled"], mb["correct"], mb["n_labelled"])
        z_result["primary_metric"] = "accuracy"
    elif ma["approval_rate"] is not None and mb["approval_rate"] is not None:
        # Reconstruct successes from rate * n for the approval-rate read.
        z_result = two_proportion_z_test(
            round(ma["approval_rate"] * ma["n"]), ma["n"],
            round(mb["approval_rate"] * mb["n"]), mb["n"],
        )
        z_result["primary_metric"] = "approval_rate"

    if z_result.get("p_value") is not None:
        verdict = "SIGNIFICANT" if z_result["p_value"] < 0.05 else "not significant"
        print(f"Primary metric ({z_result['primary_metric']}): "
              f"z={z_result['z']:.3f} p={z_result['p_value']:.4f} → {verdict}")
        print("This is a report only — promotion still requires the approval gate.")

# COMMAND ----------

# DBTITLE 1,Persist the comparison
from datetime import datetime, timezone

checked_at = datetime.now(timezone.utc).isoformat()
rows = []
for arm, m in metrics.items():
    rows.append((
        m["variant"], str(m["model_version"]), int(m["n"]),
        float(m["approval_rate"]) if m["approval_rate"] is not None else None,
        float(m["positive_pred_rate"]) if m["positive_pred_rate"] is not None else None,
        float(m["accuracy"]) if m["accuracy"] is not None else None,
        float(m["precision"]) if m["precision"] is not None else None,
        float(m["recall"]) if m["recall"] is not None else None,
        float(psi_by_arm[arm]) if psi_by_arm[arm] is not None else None,
        z_result.get("primary_metric"),
        float(z_result["z"]) if z_result.get("z") is not None else None,
        float(z_result["p_value"]) if z_result.get("p_value") is not None else None,
        "batch",
        checked_at,
    ))

schema = (
    "variant string, model_version string, n bigint, approval_rate double, "
    "positive_pred_rate double, accuracy double, precision double, recall double, "
    "psi double, primary_metric string, z double, p_value double, "
    "serving_mode string, checked_at string"
)
result_df = spark.createDataFrame(rows, schema)
(
    result_df.write.format("delta")
    .mode("append")
    .option("mergeSchema", "true")
    .saveAsTable(names.ab_comparison_results)
)
print(f"Wrote {len(rows)} arm rows to {names.ab_comparison_results}")

dbutils.notebook.exit(f"COMPARED:{','.join(sorted(metrics))}")
