"""Business policy as Unity Catalog SQL functions.

There is a hard boundary between what the model does and what the business does:

* The **model** predicts a probability of default. That is all it does. It does not decide
  whether to grant credit.
* The **decision layer** turns that probability into an action, applying eligibility
  filters, risk bands and limit calculations.

Keeping them separate has a concrete payoff. Policy changes far more often than models —
a regulator caps a limit, risk tightens a band — and with this split those changes deploy
as a SQL function update, with no retraining, no re-scoring and no model risk review.

Registering the rules as UC functions (rather than burying them in notebook code) makes
them governed objects: versioned, grantable, lineage-tracked, and callable identically
from batch jobs, SQL dashboards and ad-hoc queries.
"""

# Rejection reasons are enumerated rather than free text, because a regulator asking "why
# was this applicant declined?" needs a deterministic, machine-checkable answer.
REJECTION_REASONS = (
    "KYC_NOT_VERIFIED",
    "UNDERAGE",
    "PD_EXCEEDS_THRESHOLD",
)

# PD cut-offs defining the risk bands. Ordered low to high risk.
BAND_THRESHOLDS = {
    "BAND_A_LOW_RISK": 0.10,
    "BAND_B_MEDIUM_RISK": 0.25,
    "BAND_C_HIGH_RISK": 0.50,
}

MINIMUM_AGE = 18


def eligibility_function_ddl(function_name: str) -> str:
    """DDL for the eligibility + risk-band function.

    Hard filters are evaluated before the probability bands: a KYC failure or an underage
    applicant is rejected regardless of how good the model score is. Encoding that order
    in the function guarantees a low PD can never override a regulatory filter.
    """
    return f"""
CREATE OR REPLACE FUNCTION {function_name}(
  pd_score DOUBLE,
  age INT,
  kyc_status STRING
)
RETURNS STRUCT<eligible: BOOLEAN, risk_band: STRING, rejection_reason: STRING>
LANGUAGE SQL
DETERMINISTIC
COMMENT 'Applies credit-policy hard filters, then assigns a PD-based risk band.'
RETURN CASE
  -- Hard regulatory filters first: these cannot be overridden by a good model score.
  WHEN kyc_status IS NULL OR upper(kyc_status) != 'VERIFIED' THEN
    named_struct('eligible', false, 'risk_band', 'REJECTED', 'rejection_reason', 'KYC_NOT_VERIFIED')
  WHEN age IS NULL OR age < {MINIMUM_AGE} THEN
    named_struct('eligible', false, 'risk_band', 'REJECTED', 'rejection_reason', 'UNDERAGE')
  -- Probability-of-default bands.
  WHEN pd_score <= {BAND_THRESHOLDS['BAND_A_LOW_RISK']} THEN
    named_struct('eligible', true, 'risk_band', 'BAND_A_LOW_RISK', 'rejection_reason', 'NONE')
  WHEN pd_score <= {BAND_THRESHOLDS['BAND_B_MEDIUM_RISK']} THEN
    named_struct('eligible', true, 'risk_band', 'BAND_B_MEDIUM_RISK', 'rejection_reason', 'NONE')
  WHEN pd_score <= {BAND_THRESHOLDS['BAND_C_HIGH_RISK']} THEN
    named_struct('eligible', true, 'risk_band', 'BAND_C_HIGH_RISK', 'rejection_reason', 'NONE')
  ELSE
    named_struct('eligible', false, 'risk_band', 'BAND_D_EXCESSIVE_RISK', 'rejection_reason', 'PD_EXCEEDS_THRESHOLD')
END
"""


def credit_limit_function_ddl(function_name: str) -> str:
    """DDL for the credit-limit calculation.

    Limits are a multiple of income, capped per band. The cap is what bounds worst-case
    exposure if the model under-predicts risk for a high-income applicant — the model's
    score sets the band, but policy sets the ceiling.
    """
    return f"""
CREATE OR REPLACE FUNCTION {function_name}(
  risk_band STRING,
  credit_amount DOUBLE
)
RETURNS DOUBLE
LANGUAGE SQL
DETERMINISTIC
COMMENT 'Recommended credit limit from risk band and requested amount, capped per band.'
RETURN CASE
  WHEN risk_band = 'BAND_A_LOW_RISK'    THEN LEAST(credit_amount * 2.0, 1000000.0)
  WHEN risk_band = 'BAND_B_MEDIUM_RISK' THEN LEAST(credit_amount * 1.0,  500000.0)
  WHEN risk_band = 'BAND_C_HIGH_RISK'   THEN LEAST(credit_amount * 0.5,  100000.0)
  -- Rejected or excessive-risk applicants receive no limit.
  ELSE 0.0
END
"""


def band_for_pd(pd_score: float) -> str:
    """Pure-Python mirror of the SQL band logic, for unit testing.

    This duplicates the thresholds intentionally: the test suite asserts that this and the
    SQL function agree, which is what catches a band being changed in one place and not the
    other.
    """
    if pd_score <= BAND_THRESHOLDS["BAND_A_LOW_RISK"]:
        return "BAND_A_LOW_RISK"
    if pd_score <= BAND_THRESHOLDS["BAND_B_MEDIUM_RISK"]:
        return "BAND_B_MEDIUM_RISK"
    if pd_score <= BAND_THRESHOLDS["BAND_C_HIGH_RISK"]:
        return "BAND_C_HIGH_RISK"
    return "BAND_D_EXCESSIVE_RISK"


def evaluate_eligibility(pd_score: float, age: int, kyc_status: str) -> dict:
    """Pure-Python mirror of the eligibility function, for unit testing."""
    if not kyc_status or kyc_status.upper() != "VERIFIED":
        return {"eligible": False, "risk_band": "REJECTED", "rejection_reason": "KYC_NOT_VERIFIED"}
    if age is None or age < MINIMUM_AGE:
        return {"eligible": False, "risk_band": "REJECTED", "rejection_reason": "UNDERAGE"}
    band = band_for_pd(pd_score)
    if band == "BAND_D_EXCESSIVE_RISK":
        return {
            "eligible": False,
            "risk_band": band,
            "rejection_reason": "PD_EXCEEDS_THRESHOLD",
        }
    return {"eligible": True, "risk_band": band, "rejection_reason": "NONE"}
