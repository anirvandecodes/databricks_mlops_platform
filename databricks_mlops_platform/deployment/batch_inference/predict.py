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
    import mlflow

    source = spark.table(input_table)

    # Feature columns only: identifiers and bookkeeping columns are not model inputs, and
    # passing them would break the model signature.
    exclude = {
        "customer_id",
        "computed_at",
        "scoring_batch_date",
        "class",
        "residence_band",
        "age_band",
        "env",
        "ingested_at",
    }
    feature_columns = [c for c in source.columns if c not in exclude]

    predict_udf = mlflow.pyfunc.spark_udf(spark, model_uri=model_uri, result_type="double")

    return (
        source.withColumn("prediction", predict_udf(F.struct(*feature_columns)))
        .withColumn("model_version", F.lit(str(model_version)))
        .withColumn("scored_at", F.current_timestamp())
    )


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


def append_inference_log(
    predictions: DataFrame,
    inference_log_table: str,
    monitored_columns: list,
    decision_threshold: float,
) -> int:
    """Append this batch to the append-only inference log.

    The log is narrowed to the monitored feature set plus prediction metadata. Logging
    every column would make the monitor expensive and its output hard to read, and drift
    on a column nobody governs is not actionable.

    ``prediction`` is written as the **predicted class** (0/1) at ``decision_threshold``,
    not as the raw probability. The monitor is configured with a classification problem
    type, so it compares ``prediction_col`` against ``label_col`` directly: a probability
    there never equals a 0/1 label, which silently yields accuracy 0.0 and a meaningless
    confusion matrix rather than an error. The probability is kept alongside as
    ``prediction_score``, since a drift investigation needs the score distribution and
    thresholding throws that away.

    The threshold is passed in rather than defaulted here: it is the same risk parameter
    validation gates on, and a second copy would let the two drift apart.

    ``ground_truth`` is written as a typed null placeholder so the monitor's schema is
    stable from day one; labels are joined in later as outcomes mature
    (``monitoring.label_join``).
    """
    available = [c for c in monitored_columns if c in predictions.columns]

    log_df = (
        predictions.select(
            *(["customer_id"] if "customer_id" in predictions.columns else []),
            *available,
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

    (
        log_df.write.format("delta")
        .mode("append")
        .option("mergeSchema", "true")
        .saveAsTable(inference_log_table)
    )
    return log_df.count()
