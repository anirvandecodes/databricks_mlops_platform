"""Shared pytest fixtures.

Spark session strategy — Databricks Connect first, local Spark as fallback:

Dev machines here run ``databricks-connect``, whose ``pyspark`` deliberately refuses to
create a local session. Rather than fight that, the ``spark`` fixture prefers a
Connect session against serverless compute: transforms are then exercised by the same
engine that runs them in production, which catches Connect-incompatible constructs (RDD
access, ``spark.sparkContext``, some UDF patterns) that a local session would happily
accept and a job would then fail on.

Set ``MLOPS_TEST_PROFILE`` to pick the auth profile (default: ``DEFAULT``, i.e. whatever
``DATABRICKS_CONFIG_PROFILE`` / env vars already select). Where a Connect session cannot
be established, the fixture falls back to local Spark, and skips only if neither works —
so CI runners with vanilla pyspark and no workspace credentials still run this tier.

The session is module-scoped: Connect session startup is the dominant cost, so creating
one per test would make the fast tier slow enough that people stop running it.
"""
import os

import pytest

_SESSION_CACHE = {}


def _connect_session():
    """Try to build a Databricks Connect session against serverless compute."""
    from databricks.connect import DatabricksSession

    builder = DatabricksSession.builder.serverless(True)
    profile = os.environ.get("MLOPS_TEST_PROFILE")
    if profile:
        builder = builder.profile(profile)
    return builder.getOrCreate()


def _local_session():
    """Fall back to a local Spark session (vanilla pyspark installs, CI runners)."""
    from pyspark.sql import SparkSession

    return (
        SparkSession.builder.master("local[2]")
        .appName("mlops-platform-unit-tests")
        # Shuffle partitions default to 200, which is pure overhead on tiny in-memory
        # frames and dominates test runtime.
        .config("spark.sql.shuffle.partitions", "2")
        .config("spark.ui.enabled", "false")
        .getOrCreate()
    )


@pytest.fixture(scope="module")
def spark():
    """SparkSession for feature-transform tests (Connect preferred, local fallback)."""
    if "session" in _SESSION_CACHE:
        return _SESSION_CACHE["session"]

    failures = []
    for label, factory in (("databricks-connect", _connect_session), ("local", _local_session)):
        try:
            session = factory()
            # Force a real round-trip: a builder can succeed while the compute behind it
            # is unreachable, and we want that to surface here rather than mid-test.
            session.range(1).count()
        except Exception as exc:
            failures.append(f"{label}: {type(exc).__name__}: {exc}")
            continue
        print(f"\n[tests] Spark session via {label}")
        _SESSION_CACHE["session"] = session
        return session

    pytest.skip("no Spark session available -> " + " | ".join(failures))


# Schema of the UCI Credit-G columns this platform's transforms depend on. The real
# dataset has 21 columns; the fixture carries the numeric ones the feature code touches
# plus the label, which is enough to exercise every transform.
_CREDIT_G_SCHEMA = (
    "duration int, credit_amount double, installment_commitment int, age int, "
    "existing_credits int, num_dependents int, residence_since int, class int"
)


@pytest.fixture
def sample_applicants(spark):
    """A small applicant set covering the edge cases the transforms must survive.

    Deliberately includes zero duration and zero age — the inputs that make naive
    ratio features emit inf or null.
    """
    rows = [
        # Typical prime applicant, stable residence.
        (24, 4500.0, 2, 35, 1, 1, 4, 0),
        # Short-duration, high-amount request from a young applicant.
        (6, 12000.0, 4, 22, 3, 2, 1, 1),
        # Degenerate duration — must not divide by zero.
        (0, 1000.0, 1, 45, 1, 0, 5, 0),
        # Nulls in optional count columns.
        (36, 8000.0, None, 52, None, None, None, 0),
        # Senior applicant, long duration, many existing credits.
        (60, 15000.0, 4, 68, 4, 1, 7, 1),
    ]
    return spark.createDataFrame(rows, _CREDIT_G_SCHEMA)
