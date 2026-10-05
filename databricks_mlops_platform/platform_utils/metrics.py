"""Drift and stability metrics.

Population Stability Index (PSI) is the metric credit-risk teams actually govern on.
Implemented here as a pure function so it is unit-testable without a cluster, and so the
formula the retraining decision rests on is version-controlled in one place.

Note that data profiling also emits ``population_stability_index`` in the monitor's
``_drift_metrics`` table. This module stays the source of the *retraining verdict* because
``DriftCheck`` must return one on every run, whereas the managed metrics appear only after
a monitor refresh and need two populated windows to compare.
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


def two_proportion_z_test(
    successes_a: int, n_a: int, successes_b: int, n_b: int
) -> dict:
    """Two-sided two-proportion z-test comparing two A/B arms.

    Answers "is the difference between arm A's rate and arm B's rate more than noise?" for
    a binary outcome — an approval rate, or a correct-prediction rate once labels mature.
    The pooled-proportion form is used, which is the standard test for equality of two
    proportions.

    Implemented in pure Python with ``math.erf`` for the normal CDF, so the analysis tier
    needs no SciPy — the same cluster-free stance as :func:`population_stability_index`.

    :return: ``{rate_a, rate_b, diff, z, p_value}``. ``diff`` is ``rate_b - rate_a`` so a
        positive value means arm B scored higher. ``z`` and ``p_value`` are ``None`` when
        the test is undefined (an empty arm, or both arms at an identical 0%/100% rate,
        where the pooled variance is zero and no difference exists to test).
    """
    if n_a <= 0 or n_b <= 0:
        return {"rate_a": None, "rate_b": None, "diff": None, "z": None, "p_value": None}

    rate_a = successes_a / n_a
    rate_b = successes_b / n_b
    diff = rate_b - rate_a

    pooled = (successes_a + successes_b) / (n_a + n_b)
    variance = pooled * (1 - pooled) * (1 / n_a + 1 / n_b)

    if variance <= 0:
        # Degenerate: both arms are all-0 or all-1, so there is no difference to test.
        return {"rate_a": rate_a, "rate_b": rate_b, "diff": diff, "z": None, "p_value": None}

    import math

    z = diff / math.sqrt(variance)
    # Two-sided p-value from the standard normal survival function, via erf.
    p_value = math.erfc(abs(z) / math.sqrt(2))

    return {"rate_a": rate_a, "rate_b": rate_b, "diff": diff, "z": z, "p_value": p_value}
