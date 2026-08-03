"""Level-1 tests: feature transforms on a local Spark session.

The point of this tier is speed and determinism — no cluster, no Unity Catalog, no
network. These are the tests that catch a null-propagation or divide-by-zero regression
before it reaches a training run.
"""
import pytest

pytest.importorskip("pyspark")

from feature_engineering.features.credit_features import (  # noqa: E402
    DERIVED_FEATURE_COLUMNS,
    compute_burden_features,
    compute_credit_per_year_age,
    compute_dependency_features,
    compute_features_fn,
    compute_monthly_instalment,
    compute_stability_features,
)


def _rows(df, column):
    return [row[column] for row in df.collect()]


# -- monthly instalment ----------------------------------------------------------


def test_monthly_instalment_amortises_amount_over_duration(spark):
    df = spark.createDataFrame(
        [(24, 4800.0), (12, 6000.0)], "duration int, credit_amount double"
    )
    assert _rows(compute_monthly_instalment(df), "monthly_instalment") == [200.0, 500.0]


def test_zero_duration_yields_zero_not_infinity(spark):
    """Divide-by-zero must be handled, not left to produce inf and poison training."""
    df = spark.createDataFrame([(0, 1000.0)], "duration int, credit_amount double")
    assert _rows(compute_monthly_instalment(df), "monthly_instalment") == [0.0]


def test_null_duration_yields_zero_not_null(spark):
    df = spark.createDataFrame([(None, 1000.0)], "duration int, credit_amount double")
    assert _rows(compute_monthly_instalment(df), "monthly_instalment") == [0.0]


# -- credit-to-age ---------------------------------------------------------------


def test_credit_to_age_ratio_is_computed(spark):
    df = spark.createDataFrame([(3500.0, 35)], "credit_amount double, age int")
    assert _rows(compute_credit_per_year_age(df), "credit_to_age_ratio") == [100.0]


def test_zero_age_is_handled(spark):
    df = spark.createDataFrame([(1000.0, 0)], "credit_amount double, age int")
    assert _rows(compute_credit_per_year_age(df), "credit_to_age_ratio") == [0.0]


# -- obligation load -------------------------------------------------------------


def test_obligation_load_scales_with_existing_credits(spark):
    df = spark.createDataFrame(
        [(2, 1), (2, 3)], "installment_commitment int, existing_credits int"
    )
    result = compute_burden_features(df)
    assert _rows(result, "total_obligation_load") == [2, 6]
    assert _rows(result, "has_multiple_credits") == [0, 1]


def test_null_counts_are_treated_as_defaults(spark):
    """Nulls must not silently zero out the whole feature via null propagation."""
    df = spark.createDataFrame(
        [(None, None)], "installment_commitment int, existing_credits int"
    )
    result = compute_burden_features(df)
    assert _rows(result, "total_obligation_load") == [0]
    assert _rows(result, "has_multiple_credits") == [0]


# -- dependents ------------------------------------------------------------------


def test_dependents_per_credit_never_divides_by_zero(spark):
    df = spark.createDataFrame(
        [(2, 0), (2, 2)], "num_dependents int, existing_credits int"
    )
    assert _rows(compute_dependency_features(df), "dependents_per_credit") == [2.0, 1.0]


# -- bands -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "years,band",
    [(0, "TRANSIENT"), (1, "TRANSIENT"), (2, "SETTLING"), (3, "SETTLING"), (4, "STABLE")],
)
def test_residence_band_boundaries(spark, years, band):
    df = spark.createDataFrame([(years, 30)], "residence_since int, age int")
    assert _rows(compute_stability_features(df), "residence_band") == [band]


@pytest.mark.parametrize(
    "age,band",
    [(18, "YOUNG"), (24, "YOUNG"), (25, "PRIME"), (39, "PRIME"), (40, "MATURE"), (60, "SENIOR")],
)
def test_age_band_boundaries(spark, age, band):
    df = spark.createDataFrame([(3, age)], "residence_since int, age int")
    assert _rows(compute_stability_features(df), "age_band") == [band]


# -- full pipeline ---------------------------------------------------------------


def test_full_pipeline_produces_every_declared_feature(spark, sample_applicants):
    """Guards against a feature being declared but never actually computed."""
    result = compute_features_fn(sample_applicants)
    missing = [c for c in DERIVED_FEATURE_COLUMNS if c not in result.columns]
    assert missing == [], f"declared features never produced: {missing}"


def test_full_pipeline_preserves_row_count(spark, sample_applicants):
    """Feature engineering must enrich rows, never silently drop applicants."""
    before = sample_applicants.count()
    assert compute_features_fn(sample_applicants).count() == before


def test_full_pipeline_preserves_the_label(spark, sample_applicants):
    """Dropping the label would make training silently impossible."""
    assert "class" in compute_features_fn(sample_applicants).columns


def test_full_pipeline_leaves_no_nulls_in_derived_features(spark, sample_applicants):
    from pyspark.sql import functions as F

    result = compute_features_fn(sample_applicants)
    null_counts = result.select(
        [F.sum(F.col(c).isNull().cast("int")).alias(c) for c in DERIVED_FEATURE_COLUMNS]
    ).collect()[0]
    assert all(
        null_counts[c] == 0 for c in DERIVED_FEATURE_COLUMNS
    ), dict(null_counts.asDict())


def test_full_pipeline_is_idempotent(spark, sample_applicants):
    """Re-running on its own output must not change values — matters for retries."""
    once = compute_features_fn(sample_applicants)
    twice = compute_features_fn(once)
    assert _rows(once, "monthly_instalment") == _rows(twice, "monthly_instalment")
