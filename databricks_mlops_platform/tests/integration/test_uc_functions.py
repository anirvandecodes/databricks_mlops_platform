"""Level-2 integration tests for the Unity Catalog functions.

Separate from test_governance_e2e.py because these need only Spark (via Databricks
Connect), not MLflow — so they run in more environments, including dev machines that have
databricks-connect but no MLflow.

What they establish: the SQL actually deployed to Unity Catalog agrees with the
pure-Python mirrors the unit tests assert against. Without this, the unit tests would be
verifying logic that is not the logic running in production.

    MLOPS_TEST_PROFILE=<profile> pytest tests/integration/test_uc_functions.py -v

Each test registers functions under a unique name and drops them afterwards, so
concurrent CI runs cannot collide.
"""
import os
import uuid

import pytest

CATALOG = os.environ.get("MLOPS_TEST_CATALOG", "paypay_demo_catalog")
SCHEMA = os.environ.get("MLOPS_TEST_SCHEMA", "payments_dev")


@pytest.fixture
def registered_policy(spark):
    """Register the policy SQL functions under unique names, then drop them."""
    from decision_layer.policy import credit_limit_function_ddl, eligibility_function_ddl

    suffix = uuid.uuid4().hex[:8]
    eligibility = f"{CATALOG}.{SCHEMA}.it_eligibility_{suffix}"
    limit = f"{CATALOG}.{SCHEMA}.it_limit_{suffix}"

    spark.sql(eligibility_function_ddl(eligibility))
    spark.sql(credit_limit_function_ddl(limit))

    yield eligibility, limit

    for function in (eligibility, limit):
        try:
            spark.sql(f"DROP FUNCTION IF EXISTS {function}")
        except Exception as exc:  # pragma: no cover - best-effort teardown
            print(f"teardown: could not drop {function}: {exc}")


@pytest.fixture
def registered_psi(spark):
    """Register the PSI function under a unique name, then drop it."""
    from platform_utils.metrics import psi_function_ddl

    name = f"{CATALOG}.{SCHEMA}.it_psi_{uuid.uuid4().hex[:8]}"
    spark.sql(psi_function_ddl(name))
    yield name
    try:
        spark.sql(f"DROP FUNCTION IF EXISTS {name}")
    except Exception as exc:  # pragma: no cover - best-effort teardown
        print(f"teardown: could not drop {name}: {exc}")


# -- policy functions ------------------------------------------------------------


def test_sql_policy_rejects_unverified_kyc(spark, registered_policy):
    """Hard filter must fire in the deployed SQL, not just in the Python mirror."""
    eligibility, _ = registered_policy
    row = spark.sql(f"SELECT {eligibility}(0.01, 40, 'PENDING') AS r").collect()[0]["r"]
    assert row["eligible"] is False
    assert row["rejection_reason"] == "KYC_NOT_VERIFIED"


def test_sql_policy_rejects_underage(spark, registered_policy):
    eligibility, _ = registered_policy
    row = spark.sql(f"SELECT {eligibility}(0.01, 16, 'VERIFIED') AS r").collect()[0]["r"]
    assert row["eligible"] is False
    assert row["rejection_reason"] == "UNDERAGE"


def test_sql_policy_handles_null_kyc(spark, registered_policy):
    """Nulls must fail closed; failing open would approve unverified applicants."""
    eligibility, _ = registered_policy
    row = spark.sql(f"SELECT {eligibility}(0.01, 40, NULL) AS r").collect()[0]["r"]
    assert row["eligible"] is False
    assert row["rejection_reason"] == "KYC_NOT_VERIFIED"


@pytest.mark.parametrize(
    "pd_score,age,kyc",
    [
        (0.05, 30, "VERIFIED"),
        (0.10, 30, "VERIFIED"),
        (0.20, 45, "VERIFIED"),
        (0.25, 45, "VERIFIED"),
        (0.40, 50, "VERIFIED"),
        (0.50, 50, "VERIFIED"),
        (0.90, 50, "VERIFIED"),
        (0.01, 16, "VERIFIED"),
        (0.01, 40, "PENDING"),
    ],
)
def test_sql_policy_matches_python_mirror(spark, registered_policy, pd_score, age, kyc):
    """SQL and Python must agree, or the tested logic is not the deployed logic."""
    from decision_layer.policy import evaluate_eligibility

    eligibility, _ = registered_policy
    row = spark.sql(f"SELECT {eligibility}({pd_score}, {age}, '{kyc}') AS r").collect()[0]["r"]
    expected = evaluate_eligibility(pd_score, age, kyc)

    assert row["eligible"] == expected["eligible"]
    assert row["risk_band"] == expected["risk_band"]
    assert row["rejection_reason"] == expected["rejection_reason"]


def test_sql_credit_limit_is_capped(spark, registered_policy):
    """The cap bounds exposure when the model under-predicts risk for a large request."""
    _, limit = registered_policy
    value = spark.sql(f"SELECT {limit}('BAND_A_LOW_RISK', 99999999.0) AS v").collect()[0]["v"]
    assert value == 1000000.0


def test_sql_credit_limit_scales_with_band(spark, registered_policy):
    """Riskier bands must never receive a larger limit than safer ones."""
    _, limit = registered_policy
    limits = {
        band: spark.sql(f"SELECT {limit}('{band}', 50000.0) AS v").collect()[0]["v"]
        for band in ("BAND_A_LOW_RISK", "BAND_B_MEDIUM_RISK", "BAND_C_HIGH_RISK")
    }
    assert limits["BAND_A_LOW_RISK"] > limits["BAND_B_MEDIUM_RISK"] > limits["BAND_C_HIGH_RISK"]


def test_sql_credit_limit_is_zero_for_rejected(spark, registered_policy):
    _, limit = registered_policy
    value = spark.sql(f"SELECT {limit}('REJECTED', 50000.0) AS v").collect()[0]["v"]
    assert value == 0.0


# -- PSI function ----------------------------------------------------------------


def test_uc_psi_matches_python_implementation(spark, registered_psi):
    """Monitoring uses the UC function; it must match the unit-tested formula."""
    from platform_utils.metrics import population_stability_index

    actual = [400.0, 50.0, 25.0, 25.0]
    expected = [100.0, 100.0, 100.0, 100.0]

    sql_value = spark.sql(
        f"SELECT {registered_psi}("
        f"array({','.join(f'{v}D' for v in actual)}), "
        f"array({','.join(f'{v}D' for v in expected)})) AS psi"
    ).collect()[0]["psi"]

    assert sql_value == pytest.approx(population_stability_index(actual, expected), rel=1e-6)


def test_uc_psi_is_zero_for_identical_distributions(spark, registered_psi):
    counts = "array(100D, 200D, 300D)"
    value = spark.sql(f"SELECT {registered_psi}({counts}, {counts}) AS psi").collect()[0]["psi"]
    assert value == pytest.approx(0.0, abs=1e-9)


def test_uc_psi_handles_zero_buckets_without_infinity(spark, registered_psi):
    """A zero bucket must be floored, not produce inf and poison the drift signal."""
    value = spark.sql(
        f"SELECT {registered_psi}(array(0D, 100D, 100D), array(50D, 75D, 75D)) AS psi"
    ).collect()[0]["psi"]
    assert value is not None
    assert value > 0
    assert value != float("inf")


def test_uc_psi_grows_with_divergence(spark, registered_psi):
    baseline = "array(100D, 100D, 100D, 100D)"
    mild = spark.sql(
        f"SELECT {registered_psi}(array(110D, 100D, 95D, 95D), {baseline}) AS psi"
    ).collect()[0]["psi"]
    severe = spark.sql(
        f"SELECT {registered_psi}(array(400D, 50D, 25D, 25D), {baseline}) AS psi"
    ).collect()[0]["psi"]
    assert severe > mild
