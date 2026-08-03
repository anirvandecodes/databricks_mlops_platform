"""Drift and stability metrics.

Population Stability Index (PSI) is the metric credit-risk teams actually govern on,
and it is not a built-in Lakehouse Monitoring metric. Implementing it here as a pure
function gives it two properties that matter: it is unit-testable without a cluster,
and it is registered once as a Unity Catalog function so every team computes drift
with the identical, audited formula.
"""
from typing import Sequence

# Floor applied to zero-count buckets. PSI takes a log ratio, so an empty bucket in
# either distribution would otherwise produce inf/NaN and silently poison the sum.
_EPSILON = 1e-4


def population_stability_index(
    actual_counts: Sequence[float], expected_counts: Sequence[float]
) -> float:
    """Compute PSI between an observed and a baseline distribution.

    PSI = sum over buckets of ``(a_i - e_i) * ln(a_i / e_i)`` where ``a`` and ``e`` are
    the bucket proportions of the actual and expected distributions.

    Conventional credit-risk reading of the result:
        < 0.10  no significant population shift
        < 0.25  moderate shift, investigate
        >= 0.25 significant shift, model likely needs retraining

    :param actual_counts: Bucket counts from the live/observed population.
    :param expected_counts: Bucket counts from the training baseline.
    :return: PSI as a non-negative float. Returns 0.0 when either input is empty.
    :raises ValueError: If the two distributions have different bucket counts.
    """
    if len(actual_counts) != len(expected_counts):
        raise ValueError(
            f"Bucket count mismatch: actual has {len(actual_counts)}, "
            f"expected has {len(expected_counts)}. PSI requires identical bucketing."
        )
    if not actual_counts:
        return 0.0

    actual_total = float(sum(actual_counts))
    expected_total = float(sum(expected_counts))
    # An all-zero distribution carries no information; report no drift rather than
    # dividing by zero.
    if actual_total <= 0 or expected_total <= 0:
        return 0.0

    import math

    psi = 0.0
    for actual, expected in zip(actual_counts, expected_counts):
        actual_pct = max(actual / actual_total, _EPSILON)
        expected_pct = max(expected / expected_total, _EPSILON)
        psi += (actual_pct - expected_pct) * math.log(actual_pct / expected_pct)
    return psi


def psi_verdict(psi: float, warn: float = 0.10, retrain: float = 0.25) -> str:
    """Map a PSI value onto the action the platform should take.

    Thresholds are passed in (sourced from bundle variables) rather than hardcoded, so
    risk owners tune them per environment via PR.
    """
    if psi >= retrain:
        return "RETRAIN"
    if psi >= warn:
        return "WARN"
    return "STABLE"


# SQL body registered as a Unity Catalog function. Registering the metric in UC — rather
# than importing a wheel into each job — means the CM and fraud teams compute PSI with
# the same audited definition, and the formula itself is version-controlled here.
PSI_UC_FUNCTION_TEMPLATE = """
CREATE OR REPLACE FUNCTION {function_name}(
  actual_counts ARRAY<DOUBLE>,
  expected_counts ARRAY<DOUBLE>
)
RETURNS DOUBLE
LANGUAGE SQL
DETERMINISTIC
COMMENT 'Population Stability Index between an observed and a baseline distribution.'
RETURN (
  SELECT COALESCE(
    SUM(
      (actual_pct - expected_pct) * LN(actual_pct / expected_pct)
    ), 0.0)
  FROM (
    SELECT
      GREATEST(a.count_value / NULLIF(totals.actual_total, 0), {epsilon}) AS actual_pct,
      GREATEST(e.count_value / NULLIF(totals.expected_total, 0), {epsilon}) AS expected_pct
    FROM
      (SELECT posexplode(actual_counts) AS (idx, count_value)) a
      JOIN (SELECT posexplode(expected_counts) AS (idx, count_value)) e
        ON a.idx = e.idx
      CROSS JOIN (
        SELECT
          aggregate(actual_counts, 0D, (acc, x) -> acc + x) AS actual_total,
          aggregate(expected_counts, 0D, (acc, x) -> acc + x) AS expected_total
      ) totals
  )
)
"""


def psi_function_ddl(function_name: str) -> str:
    """Render the ``CREATE FUNCTION`` statement for the UC-registered PSI metric."""
    return PSI_UC_FUNCTION_TEMPLATE.format(function_name=function_name, epsilon=_EPSILON)
