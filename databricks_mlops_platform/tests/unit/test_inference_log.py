"""Unit tests for what batch scoring writes to the inference log.

The property under test is the one that silently broke the monitor: a classification
monitor compares ``prediction_col`` against ``label_col`` for equality, so the log must
carry a predicted *class*, not a probability. A probability there produces accuracy 0.0
and an all-zero confusion matrix without raising anything.

These assert on the shaped DataFrame rather than a written table, so they belong in the
offline tier: the Delta write itself needs a workspace and is covered by the integration
tests.
"""
import pytest

from deployment.batch_inference.predict import build_inference_log

_SCHEMA = "customer_id string, duration bigint, prediction double, model_version string"


@pytest.fixture
def logged(spark):
    """The inference log built from predictions spanning a decision threshold of 0.30."""
    from pyspark.sql import functions as F

    rows = [
        ("c1", 12, 0.05, "3"),  # confidently repay
        ("c2", 24, 0.2999, "3"),  # just below the threshold
        ("c3", 36, 0.30, "3"),  # exactly at it -> must decline (>=)
        ("c4", 48, 0.91, "3"),  # confidently default
    ]
    scored = spark.createDataFrame(rows, _SCHEMA).withColumn(
        "scored_at", F.current_timestamp()
    )
    return build_inference_log(
        scored,
        monitored_columns=["duration"],
        decision_threshold=0.30,
    )


def test_prediction_is_logged_as_a_class_not_a_probability(logged):
    """The bug this guards: a probability never equals a 0/1 label, so accuracy reads 0."""
    values = {r["prediction"] for r in logged.select("prediction").collect()}
    assert values <= {0.0, 1.0}, f"expected binary classes, got {values}"


def test_threshold_is_inclusive_at_the_boundary(logged):
    """A score exactly at the threshold must decline, matching validation's `>=`."""
    by_id = {r["customer_id"]: r for r in logged.collect()}
    assert by_id["c2"]["prediction"] == 0.0  # 0.2999
    assert by_id["c3"]["prediction"] == 1.0  # 0.30 exactly


def test_probability_is_preserved_alongside_the_class(logged):
    """Thresholding must not discard the score a drift investigation needs."""
    by_id = {r["customer_id"]: r for r in logged.collect()}
    assert by_id["c4"]["prediction_score"] == pytest.approx(0.91)
    assert by_id["c1"]["prediction_score"] == pytest.approx(0.05)


def test_ground_truth_starts_null_and_typed(logged):
    """The placeholder keeps the monitor's schema stable before outcomes mature."""
    assert dict(logged.dtypes)["ground_truth"] == "double"
    assert logged.filter("ground_truth IS NOT NULL").count() == 0
