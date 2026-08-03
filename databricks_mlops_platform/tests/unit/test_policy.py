"""Unit tests for the credit policy rules.

Two things are asserted here. First, that the rules behave as risk intends — hard filters
beat good scores, bands land on the right side of their boundaries. Second, that the
pure-Python mirror and the generated SQL agree, which is what catches a threshold changed
in one place and not the other.
"""
import re

import pytest

from decision_layer.policy import (
    BAND_THRESHOLDS,
    MINIMUM_AGE,
    band_for_pd,
    credit_limit_function_ddl,
    eligibility_function_ddl,
    evaluate_eligibility,
)


# -- hard filters take precedence -------------------------------------------------


def test_unverified_kyc_is_rejected_despite_perfect_score():
    """A regulatory filter must not be overridable by a good model score."""
    result = evaluate_eligibility(pd_score=0.001, age=40, kyc_status="PENDING")
    assert result["eligible"] is False
    assert result["rejection_reason"] == "KYC_NOT_VERIFIED"


def test_underage_is_rejected_despite_perfect_score():
    result = evaluate_eligibility(pd_score=0.001, age=17, kyc_status="VERIFIED")
    assert result["eligible"] is False
    assert result["rejection_reason"] == "UNDERAGE"


def test_kyc_is_checked_before_age():
    """Ordering matters for the reason code an auditor sees."""
    result = evaluate_eligibility(pd_score=0.01, age=15, kyc_status="PENDING")
    assert result["rejection_reason"] == "KYC_NOT_VERIFIED"


def test_missing_kyc_status_is_rejected_not_defaulted_to_pass():
    """A null must fail closed; failing open would approve unverified applicants."""
    for value in (None, "", "   "):
        assert evaluate_eligibility(0.01, 40, value)["eligible"] is False


def test_kyc_check_is_case_insensitive():
    assert evaluate_eligibility(0.01, 40, "verified")["eligible"] is True


def test_minimum_age_boundary_is_inclusive():
    assert evaluate_eligibility(0.05, MINIMUM_AGE, "VERIFIED")["eligible"] is True
    assert evaluate_eligibility(0.05, MINIMUM_AGE - 1, "VERIFIED")["eligible"] is False


# -- risk bands -------------------------------------------------------------------


@pytest.mark.parametrize(
    "pd_score,band",
    [
        (0.00, "BAND_A_LOW_RISK"),
        (0.10, "BAND_A_LOW_RISK"),  # boundaries are inclusive on the lower-risk side
        (0.11, "BAND_B_MEDIUM_RISK"),
        (0.25, "BAND_B_MEDIUM_RISK"),
        (0.26, "BAND_C_HIGH_RISK"),
        (0.50, "BAND_C_HIGH_RISK"),
        (0.51, "BAND_D_EXCESSIVE_RISK"),
        (1.00, "BAND_D_EXCESSIVE_RISK"),
    ],
)
def test_band_boundaries(pd_score, band):
    assert band_for_pd(pd_score) == band


def test_bands_are_monotonic_in_pd():
    """Higher predicted risk must never yield a lower-risk band."""
    order = [
        "BAND_A_LOW_RISK",
        "BAND_B_MEDIUM_RISK",
        "BAND_C_HIGH_RISK",
        "BAND_D_EXCESSIVE_RISK",
    ]
    seen = [order.index(band_for_pd(i / 100)) for i in range(101)]
    assert seen == sorted(seen)


def test_excessive_risk_band_is_not_eligible():
    result = evaluate_eligibility(pd_score=0.9, age=40, kyc_status="VERIFIED")
    assert result["eligible"] is False
    assert result["rejection_reason"] == "PD_EXCEEDS_THRESHOLD"


def test_eligible_applicants_carry_no_rejection_reason():
    result = evaluate_eligibility(pd_score=0.05, age=30, kyc_status="VERIFIED")
    assert result["eligible"] is True
    assert result["rejection_reason"] == "NONE"


# -- SQL / Python parity ----------------------------------------------------------


def _numbers_in(sql):
    return {float(match) for match in re.findall(r"\d+\.\d+", sql)}


def test_sql_eligibility_uses_the_same_thresholds_as_python():
    """Guards against the SQL and the Python mirror drifting apart."""
    sql = eligibility_function_ddl("cat.sch.evaluate_eligibility")
    present = _numbers_in(sql)
    for threshold in BAND_THRESHOLDS.values():
        assert threshold in present, f"threshold {threshold} missing from generated SQL"


def test_sql_eligibility_encodes_the_minimum_age():
    sql = eligibility_function_ddl("cat.sch.evaluate_eligibility")
    assert f"age < {MINIMUM_AGE}" in sql


def test_sql_functions_are_deterministic_and_replaceable():
    """DETERMINISTIC lets Spark optimise; OR REPLACE keeps deploys idempotent."""
    for ddl in (
        eligibility_function_ddl("cat.sch.f"),
        credit_limit_function_ddl("cat.sch.g"),
    ):
        assert "CREATE OR REPLACE FUNCTION" in ddl
        assert "DETERMINISTIC" in ddl
        assert "COMMENT" in ddl


def test_sql_evaluates_hard_filters_before_bands():
    """Order in the CASE expression is the control; assert it explicitly."""
    sql = eligibility_function_ddl("cat.sch.f")
    assert sql.index("KYC_NOT_VERIFIED") < sql.index("BAND_A_LOW_RISK")
    assert sql.index("UNDERAGE") < sql.index("BAND_A_LOW_RISK")


def test_credit_limit_caps_every_eligible_band():
    """Every band that can receive credit must have an absolute ceiling."""
    sql = credit_limit_function_ddl("cat.sch.g")
    for band in ("BAND_A_LOW_RISK", "BAND_B_MEDIUM_RISK", "BAND_C_HIGH_RISK"):
        assert band in sql
    # LEAST(...) is what enforces the cap; one per eligible band.
    assert sql.count("LEAST(") == 3


def test_rejected_applicants_get_zero_limit():
    sql = credit_limit_function_ddl("cat.sch.g")
    assert "ELSE 0.0" in sql
