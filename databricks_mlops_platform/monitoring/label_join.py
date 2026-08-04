"""Label backfill for the inference log.

Batch scoring writes ``ground_truth`` as a typed null placeholder (see
``deployment.batch_inference.predict.append_inference_log``) because the outcome of a
credit decision is not known at scoring time. Something has to fill that column in
later, or the monitor's model-quality half never computes: accuracy, precision, recall
and the confusion matrix all derive from ``label_col``, so an all-null label column
leaves those panels empty while the data-profiling half looks perfectly healthy.

That is the gap this module closes. It is deliberately a *separate* step from scoring
rather than part of it — labels mature on their own schedule, long after the prediction
was served, and coupling the two would mean either delaying predictions or discarding
late-arriving outcomes.

Two properties matter and are asserted by the unit tests:

* **Idempotent.** Only rows whose label is still null are updated, so re-running after a
  partial outcome load fills the new labels and leaves settled ones untouched.
* **Never overwrites a recorded label.** An outcome that has already been written is
  audit evidence; a re-run must not silently revise history.
"""
from pyspark.sql import SparkSession


def build_label_merge_sql(
    inference_log_table: str,
    ground_truth_table: str,
    key_col: str = "customer_id",
    label_col: str = "ground_truth",
    outcome_col: str = "is_default",
) -> str:
    """Build the MERGE that backfills matured labels into the inference log.

    Kept as a pure string builder so the merge semantics can be asserted without a
    cluster, and so the exact statement can be logged as promotion evidence.

    The ``WHEN MATCHED AND t.<label> IS NULL`` clause carries the whole idempotency
    guarantee: without it the merge would rewrite every historical label on every run.

    :param outcome_col: Column on the outcomes table holding the observed 0/1 result.
        Cast to double to match the inference log's ``label_col`` type, which the monitor
        pins at table-creation time.
    """
    return f"""
        MERGE INTO {inference_log_table} AS t
        USING {ground_truth_table} AS s
           ON t.{key_col} = s.{key_col}
         WHEN MATCHED AND t.{label_col} IS NULL
         THEN UPDATE SET t.{label_col} = CAST(s.{outcome_col} AS DOUBLE)
    """.strip()


def join_ground_truth(
    spark: SparkSession,
    inference_log_table: str,
    ground_truth_table: str,
    key_col: str = "customer_id",
    label_col: str = "ground_truth",
    outcome_col: str = "is_default",
) -> dict:
    """Backfill matured labels and report what changed.

    :return: Counts of labelled and still-unlabelled rows after the merge. Reported
        rather than logged-and-discarded because "how much of the log is labelled" is
        the number that tells you whether the model-quality metrics can be trusted yet.
    """
    if not spark.catalog.tableExists(ground_truth_table):
        raise RuntimeError(
            f"{ground_truth_table} does not exist. Labels cannot be joined until "
            "outcomes are being recorded."
        )

    spark.sql(
        build_label_merge_sql(
            inference_log_table=inference_log_table,
            ground_truth_table=ground_truth_table,
            key_col=key_col,
            label_col=label_col,
            outcome_col=outcome_col,
        )
    )

    return label_coverage(spark, inference_log_table, label_col=label_col)


def label_coverage(
    spark: SparkSession,
    inference_log_table: str,
    label_col: str = "ground_truth",
) -> dict:
    """Report how much of the inference log carries a matured label."""
    from pyspark.sql import functions as F

    row = spark.table(inference_log_table).select(
        F.count("*").alias("total"),
        F.count(F.col(label_col)).alias("labelled"),
    ).collect()[0]

    total = int(row["total"])
    labelled = int(row["labelled"])
    return {
        "total": total,
        "labelled": labelled,
        "unlabelled": total - labelled,
        # Guarded: an empty log is a legitimate first-deployment state, not an error.
        "coverage": round(labelled / total, 4) if total else 0.0,
    }
