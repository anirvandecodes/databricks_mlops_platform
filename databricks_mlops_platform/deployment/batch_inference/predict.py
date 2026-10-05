"""Batch scoring logic.

Kept out of the notebook so it can be reasoned about and tested directly. Two properties
matter here and are asserted by the integration tests:

* The model is resolved by **alias**, so scoring always follows whatever version is
  currently champion — including immediately after a rollback.
* Every scored row is appended to an inference log, which is the table the data profiling
  monitor attaches to. Scoring without logging would leave the platform blind to drift.
"""
from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F


# Identifiers and bookkeeping columns are not model inputs; passing them would break the
# model signature. Also excludes the A/B ``variant`` label, which is assigned before scoring
# and must not be fed to the model.
_NON_FEATURE_COLUMNS = {
    "customer_id",
    "computed_at",
    "scoring_batch_date",
    "class",
    "residence_band",
    "age_band",
    "env",
    "ingested_at",
    "variant",
}


def _score_df(
    spark: SparkSession,
    source: DataFrame,
    model_uri: str,
    model_version: str,
) -> DataFrame:
    """Score an already-loaded frame with the model at ``model_uri``.

    Split out from :func:`score_batch` so the A/B path can score a per-arm subset with a
    different model without re-reading the table. Adds ``prediction``, ``model_version``
    and ``scored_at``; any other pre-existing column (e.g. an A/B ``variant``) is preserved.
    """
    import mlflow

    feature_columns = [c for c in source.columns if c not in _NON_FEATURE_COLUMNS]

    predict_udf = mlflow.pyfunc.spark_udf(spark, model_uri=model_uri, result_type="double")

    return (
        source.withColumn("prediction", predict_udf(F.struct(*feature_columns)))
        .withColumn("model_version", F.lit(str(model_version)))
        .withColumn("scored_at", F.current_timestamp())
    )


def score_batch(
    spark: SparkSession,
    model_uri: str,
    input_table: str,
    model_version: str,
) -> DataFrame:
    """Score an input table with the model at ``model_uri``.

    :param model_uri: Alias-based URI, e.g. ``models:/cat.schema.model@champion``.
    :param model_version: Concrete version behind the alias, resolved by the caller and
        stamped on every row. Without it, a drift investigation could not attribute a
        prediction to the model that produced it.
    :return: Input columns plus ``prediction``, ``model_version`` and ``scored_at``.
    """
    return _score_df(spark, spark.table(input_table), model_uri, model_version)


def assign_variants(
    df: DataFrame,
    split_pct: int,
    salt: str,
    id_col: str = "customer_id",
) -> DataFrame:
    """Add an A/B ``variant`` column, deterministically by ``id_col``.

    See ``platform_utils.variants``: assignment is a pure function of the id and salt, so a
    customer's arm is stable across daily batches.
    """
    from platform_utils.variants import VARIANT_COL, variant_column_expr

    return df.withColumn(VARIANT_COL, variant_column_expr(id_col, split_pct, salt))


def score_batch_ab(
    spark: SparkSession,
    input_table: str,
    arms: list,
    split_pct: int,
    salt: str,
    id_col: str = "customer_id",
) -> DataFrame:
    """Score one batch across two A/B arms, each with its own model.

    Each customer is assigned an arm once (deterministically), then each arm's subset is
    scored with that arm's model and the results are unioned. Every row carries the
    ``variant`` label alongside the ``model_version`` that produced it, so the inference log
    and the monitor can compare the arms.

    :param arms: ``[(variant_label, model_uri, model_version), ...]`` — one entry per arm.
    :return: The same columns :func:`score_batch` produces, plus ``variant``.
    """
    from platform_utils.variants import VARIANT_COL

    assigned = assign_variants(spark.table(input_table), split_pct, salt, id_col=id_col)

    scored_arms = []
    for label, model_uri, model_version in arms:
        arm_source = assigned.filter(F.col(VARIANT_COL) == F.lit(label))
        scored_arms.append(_score_df(spark, arm_source, model_uri, model_version))

    result = scored_arms[0]
    for other in scored_arms[1:]:
        result = result.unionByName(other)
    return result


def write_predictions(predictions: DataFrame, output_table: str) -> int:
    """Overwrite the current-batch predictions table.

    Overwrite (not append) because this table represents "the latest scoring run"; the
    durable history lives in the inference log.
    """
    (
        predictions.write.format("delta")
        .mode("overwrite")
        .option("overwriteSchema", "true")
        .saveAsTable(output_table)
    )
    return predictions.count()


def build_inference_log(
    predictions: DataFrame,
    monitored_columns: list,
    decision_threshold: float,
) -> DataFrame:
    """Shape a scored batch into the inference log's schema.

    Split from the write so the log's content can be asserted without Delta on the
    classpath — the properties below are the ones that silently broke the monitor, and
    they belong in the offline test tier rather than one needing a workspace.

    The log is narrowed to the monitored feature set plus prediction metadata. Logging
    every column would make the monitor expensive and its output hard to read, and drift
    on a column nobody governs is not actionable.

    ``prediction`` is the **predicted class** (0/1) at ``decision_threshold``, not the raw
    probability. The monitor is configured with a classification problem type, so it
    compares ``prediction_col`` against ``label_col`` directly: a probability there never
    equals a 0/1 label, which silently yields accuracy 0.0 and a meaningless confusion
    matrix rather than an error. The probability is kept alongside as ``prediction_score``,
    since a drift investigation needs the score distribution and thresholding throws that
    away.

    The threshold is passed in rather than defaulted here: it is the same risk parameter
    validation gates on, and a second copy would let the two drift apart.

    ``ground_truth`` is a typed null placeholder so the monitor's schema is stable from day
    one; labels are joined in later as outcomes mature (``monitoring.label_join``).
    """
    available = [c for c in monitored_columns if c in predictions.columns]

    return (
        predictions.select(
            *(["customer_id"] if "customer_id" in predictions.columns else []),
            *available,
            # The A/B arm label, when scoring ran in experiment mode. Additive: a
            # champion-only batch has no variant, and the log is built without one.
            *(["variant"] if "variant" in predictions.columns else []),
            "prediction",
            "model_version",
            "scored_at",
        )
        .withColumn("prediction_score", F.col("prediction").cast("double"))
        .withColumn(
            "prediction",
            (F.col("prediction") >= F.lit(decision_threshold)).cast("double"),
        )
        .withColumn("ground_truth", F.lit(None).cast("double"))
    )


def append_inference_log(
    predictions: DataFrame,
    inference_log_table: str,
    monitored_columns: list,
    decision_threshold: float,
) -> int:
    """Append this batch to the append-only inference log.

    See :func:`build_inference_log` for what the log carries and why.
    """
    log_df = build_inference_log(predictions, monitored_columns, decision_threshold)

    (
        log_df.write.format("delta")
        .mode("append")
        .option("mergeSchema", "true")
        .saveAsTable(inference_log_table)
    )
    return log_df.count()
