"""Credit-risk feature transforms for the UCI Credit-G ("German Credit") dataset.

Pure DataFrame-in / DataFrame-out functions. Keeping them free of widgets, table names
and I/O is what makes them testable on a local Spark session in seconds — the Level-1
tier of the testing strategy — instead of only on a cluster.

Base Credit-G columns used here: ``duration`` (months), ``credit_amount``,
``installment_commitment`` (% of disposable income), ``age``, ``existing_credits``,
``num_dependents``, ``residence_since``, plus the ``class`` label (1 = default).
"""
from pyspark.sql import DataFrame
from pyspark.sql import functions as F

# Applicants with degenerate inputs (zero duration, zero income proxy) would otherwise
# divide by zero. Handled explicitly rather than left to emit nulls or inf that
# propagate silently into training.
_SAFE_DEFAULT = 0.0

LABEL_COLUMN = "class"


def compute_monthly_instalment(df: DataFrame) -> DataFrame:
    """Add ``monthly_instalment``: credit amount amortised over the loan duration.

    Credit-G gives amount and duration separately; the per-month burden is the more
    predictive form and is what a credit officer actually reasons about.
    """
    return df.withColumn(
        "monthly_instalment",
        F.when(
            (F.col("duration").isNull()) | (F.col("duration") <= 0),
            F.lit(_SAFE_DEFAULT),
        ).otherwise(F.round(F.col("credit_amount") / F.col("duration"), 2)),
    )


def compute_credit_per_year_age(df: DataFrame) -> DataFrame:
    """Add ``credit_to_age_ratio``: exposure relative to applicant age.

    A proxy for whether the requested exposure is plausible for the applicant's life
    stage — young applicants requesting large amounts are a known risk signal.
    """
    return df.withColumn(
        "credit_to_age_ratio",
        F.when(
            (F.col("age").isNull()) | (F.col("age") <= 0), F.lit(_SAFE_DEFAULT)
        ).otherwise(F.round(F.col("credit_amount") / F.col("age"), 2)),
    )


def compute_burden_features(df: DataFrame) -> DataFrame:
    """Add instalment-burden features.

    ``installment_commitment`` in Credit-G is already a percentage of disposable income;
    combining it with the number of existing credits approximates total obligation load.
    """
    return df.withColumn(
        "total_obligation_load",
        F.coalesce(F.col("installment_commitment"), F.lit(0))
        * F.coalesce(F.col("existing_credits"), F.lit(1)),
    ).withColumn(
        "has_multiple_credits",
        (F.coalesce(F.col("existing_credits"), F.lit(0)) > 1).cast("int"),
    )


def compute_dependency_features(df: DataFrame) -> DataFrame:
    """Add household-dependency features."""
    return df.withColumn(
        "dependents_per_credit",
        F.round(
            F.coalesce(F.col("num_dependents"), F.lit(0))
            / F.greatest(F.coalesce(F.col("existing_credits"), F.lit(1)), F.lit(1)),
            2,
        ),
    )


def compute_stability_features(df: DataFrame) -> DataFrame:
    """Add residential-stability features, bucketed into bands reviewers reason about."""
    return df.withColumn(
        "residence_band",
        F.when(F.coalesce(F.col("residence_since"), F.lit(0)) < 2, F.lit("TRANSIENT"))
        .when(F.col("residence_since") < 4, F.lit("SETTLING"))
        .otherwise(F.lit("STABLE")),
    ).withColumn(
        "age_band",
        F.when(F.col("age") < 25, F.lit("YOUNG"))
        .when(F.col("age") < 40, F.lit("PRIME"))
        .when(F.col("age") < 60, F.lit("MATURE"))
        .otherwise(F.lit("SENIOR")),
    )


def compute_features_fn(input_df: DataFrame) -> DataFrame:
    """Apply the full credit feature pipeline.

    Single entry point for the feature-engineering job, so the job never needs to know
    which individual transforms exist.
    """
    df = compute_monthly_instalment(input_df)
    df = compute_credit_per_year_age(df)
    df = compute_burden_features(df)
    df = compute_dependency_features(df)
    df = compute_stability_features(df)
    return df


# Engineered columns this module adds on top of the Credit-G base features. Declared
# beside the transforms that produce them, so a feature added above but never wired into
# training is an obvious omission.
DERIVED_FEATURE_COLUMNS = [
    "monthly_instalment",
    "credit_to_age_ratio",
    "total_obligation_load",
    "has_multiple_credits",
    "dependents_per_credit",
]

# Categorical bands kept for slicing monitoring metrics, not fed to the model directly.
DERIVED_CATEGORICAL_COLUMNS = ["residence_band", "age_band"]

# Drift is monitored on these. A narrow, explicit list keeps monitoring cost bounded and
# makes a PSI alert interpretable — the columns are the ones risk owners recognise.
MONITORED_FEATURE_COLUMNS = [
    "duration",
    "credit_amount",
    "age",
    "installment_commitment",
    "monthly_instalment",
    "credit_to_age_ratio",
]
