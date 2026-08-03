"""Unit tests for drift metrics — no Spark, no cluster, fast enough for pre-commit."""
import pytest

from platform_utils.metrics import population_stability_index, psi_verdict


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
