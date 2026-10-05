"""Unit tests for drift metrics — no Spark, no cluster, fast enough for pre-commit."""
import pytest

from platform_utils.metrics import (
    population_stability_index,
    psi_verdict,
    two_proportion_z_test,
)


def test_identical_distributions_have_zero_psi():
    counts = [100, 200, 300, 400]
    assert population_stability_index(counts, counts) == pytest.approx(0.0, abs=1e-9)


def test_psi_is_scale_invariant():
    """PSI compares proportions, so doubling every count must not change it."""
    actual = [50, 100, 150]
    expected = [100, 100, 100]
    single = population_stability_index(actual, expected)
    doubled = population_stability_index([c * 2 for c in actual], expected)
    assert single == pytest.approx(doubled)


def test_psi_grows_with_divergence():
    expected = [100, 100, 100, 100]
    mild = population_stability_index([110, 100, 95, 95], expected)
    severe = population_stability_index([400, 50, 25, 25], expected)
    assert severe > mild > 0


def test_psi_is_symmetric():
    """The (a-e)*ln(a/e) form is symmetric; swapping arguments must not change it."""
    a, b = [10, 20, 70], [30, 30, 40]
    assert population_stability_index(a, b) == pytest.approx(
        population_stability_index(b, a)
    )


def test_zero_bucket_does_not_produce_infinity():
    """An empty bucket must be floored, not allowed to yield inf/NaN via log(0)."""
    psi = population_stability_index([0, 100, 100], [50, 75, 75])
    assert psi > 0
    assert psi == pytest.approx(psi)  # NaN would fail this comparison
    assert psi != float("inf")


def test_empty_input_reports_no_drift():
    assert population_stability_index([], []) == 0.0


def test_all_zero_distribution_reports_no_drift():
    assert population_stability_index([0, 0], [0, 0]) == 0.0


def test_mismatched_bucket_counts_raise():
    with pytest.raises(ValueError, match="Bucket count mismatch"):
        population_stability_index([1, 2, 3], [1, 2])


@pytest.mark.parametrize(
    "psi,expected",
    [
        (0.0, "STABLE"),
        (0.09, "STABLE"),
        (0.10, "WARN"),  # boundary is inclusive on the warn side
        (0.24, "WARN"),
        (0.25, "RETRAIN"),
        (1.5, "RETRAIN"),
    ],
)
def test_psi_verdict_thresholds(psi, expected):
    assert psi_verdict(psi) == expected


def test_psi_verdict_honours_custom_thresholds():
    """Thresholds come from bundle variables, so overrides must be respected."""
    assert psi_verdict(0.05, warn=0.01, retrain=0.02) == "RETRAIN"
    assert psi_verdict(0.30, warn=0.5, retrain=0.9) == "STABLE"


# -- A/B two-proportion z-test ----------------------------------------------------


def test_z_test_diff_is_arm_b_minus_arm_a():
    """A positive diff must mean arm B scored higher, so the sign is unambiguous."""
    result = two_proportion_z_test(successes_a=50, n_a=100, successes_b=70, n_b=100)
    assert result["rate_a"] == pytest.approx(0.50)
    assert result["rate_b"] == pytest.approx(0.70)
    assert result["diff"] == pytest.approx(0.20)


def test_z_test_known_value():
    """Hand-computed: 50/100 vs 70/100, pooled p=0.6, z = 0.2 / sqrt(0.24*0.02)."""
    import math

    result = two_proportion_z_test(50, 100, 70, 100)
    expected_z = 0.20 / math.sqrt(0.6 * 0.4 * (1 / 100 + 1 / 100))
    assert result["z"] == pytest.approx(expected_z, rel=1e-6)
    assert result["p_value"] == pytest.approx(math.erfc(abs(expected_z) / math.sqrt(2)), rel=1e-6)
    # ~2.89 sigma is significant at the 5% level.
    assert result["p_value"] < 0.05


def test_z_test_is_symmetric_under_arm_swap():
    """Swapping the arms flips the z sign but leaves the two-sided p-value unchanged."""
    ab = two_proportion_z_test(50, 100, 70, 100)
    ba = two_proportion_z_test(70, 100, 50, 100)
    assert ab["z"] == pytest.approx(-ba["z"])
    assert ab["p_value"] == pytest.approx(ba["p_value"])


def test_z_test_equal_rates_are_not_significant():
    result = two_proportion_z_test(60, 100, 60, 100)
    assert result["diff"] == pytest.approx(0.0)
    assert result["z"] == pytest.approx(0.0)
    assert result["p_value"] == pytest.approx(1.0)


def test_z_test_empty_arm_is_undefined():
    result = two_proportion_z_test(0, 0, 5, 10)
    assert result["z"] is None and result["p_value"] is None


def test_z_test_degenerate_identical_extremes_are_undefined():
    """Both arms all-success: no variance, no difference to test."""
    result = two_proportion_z_test(10, 10, 20, 20)
    assert result["diff"] == pytest.approx(0.0)
    assert result["z"] is None and result["p_value"] is None
