"""Unit tests for deterministic A/B cohort assignment — pure Python, no Spark.

The properties that make an A/B experiment valid: a customer's arm is stable across runs,
the split lands at the configured percentage, and the boundaries behave. The Spark
expression's agreement with this pure logic is asserted in the integration tier, where a
real ``crc32`` is available.
"""
import pytest

from platform_utils.variants import (
    ARM_CHALLENGER,
    ARM_CHAMPION,
    BUCKETS,
    DEFAULT_SALT,
    assign_variant,
    variant_bucket,
)


def test_bucket_is_in_range():
    for cid in range(1000):
        assert 0 <= variant_bucket(cid) < BUCKETS


def test_assignment_is_deterministic():
    """The same id + salt must always yield the same arm — the core experiment property."""
    for cid in ("cust_1", "cust_2", 42, "abc-123"):
        first = assign_variant(cid, split_pct=30)
        for _ in range(50):
            assert assign_variant(cid, split_pct=30) == first


def test_split_zero_sends_everyone_to_champion():
    assert all(assign_variant(cid, split_pct=0) == ARM_CHAMPION for cid in range(2000))


def test_split_hundred_sends_everyone_to_challenger():
    assert all(assign_variant(cid, split_pct=100) == ARM_CHALLENGER for cid in range(2000))


def test_split_proportion_is_approximately_the_percentage():
    """Over many ids, arm B's share should be close to split_pct."""
    n = 100_000
    b = sum(1 for cid in range(n) if assign_variant(cid, split_pct=10) == ARM_CHALLENGER)
    share = b / n
    assert share == pytest.approx(0.10, abs=0.01), f"arm-B share {share:.4f} not ~10%"


def test_arm_membership_is_nested_in_split_pct():
    """Raising the split only ever moves customers into B, never back out.

    A customer in arm B at 10% must still be in arm B at 20% — otherwise raising the split
    would reshuffle the existing cohort, invalidating an in-flight experiment.
    """
    for cid in range(5000):
        if assign_variant(cid, split_pct=10) == ARM_CHALLENGER:
            assert assign_variant(cid, split_pct=20) == ARM_CHALLENGER


def test_changing_the_salt_reshuffles_cohorts():
    """A different salt must produce a materially different assignment."""
    n = 5000
    changed = sum(
        1
        for cid in range(n)
        if assign_variant(cid, 50, salt=DEFAULT_SALT)
        != assign_variant(cid, 50, salt="a_different_salt")
    )
    # Two independent 50/50 splits disagree on roughly half the population.
    assert 0.3 < changed / n < 0.7


def test_boundary_bucket_goes_to_challenger():
    """Bucket assignment is '< split_pct', so bucket 0 is always arm B for any positive split."""
    zero_bucket_ids = [cid for cid in range(200) if variant_bucket(cid) == 0]
    assert zero_bucket_ids, "expected at least one id hashing to bucket 0"
    for cid in zero_bucket_ids:
        assert assign_variant(cid, split_pct=1) == ARM_CHALLENGER
