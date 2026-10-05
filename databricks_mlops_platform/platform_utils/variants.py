"""Deterministic A/B experiment cohort assignment.

An A/B test needs every customer to land in a stable arm: the same applicant must be
scored by the same model on every batch, or the experiment measures noise. Assignment is
therefore a pure function of ``customer_id`` and a salt — no RNG, no timestamp, no shuffle
— so it is reproducible across runs, across the batch and analysis paths, and in tests.

The design mirrors ``decision_layer.policy``: the rule is written once as a pure-Python
function *and* as a Spark column expression, and a unit test asserts the two agree. That is
what stops the offline mirror and the in-pipeline expression drifting apart.

Assignment uses CRC-32 mod 100 because Python's ``zlib.crc32`` and Spark SQL's ``crc32``
compute the identical checksum over the same UTF-8 bytes — so the arm a customer gets is
the same whether it is decided in a notebook, in Spark, or in a test. ``split_pct`` is an
integer percentage (e.g. ``10`` for "10% into arm B"), which is exactly how the bundle
variable is expressed.

Only the batch path is deterministic per customer. The real-time serving endpoint splits
traffic stochastically per request (Databricks ``traffic_config`` cannot route by a hash),
so a customer calling the endpoint repeatedly may see either arm — see
``deployment/serving/SetupServingEndpoint.py``.
"""
import zlib

# Column and arm labels. Arm A is always the incumbent (champion); arm B is the
# challenger under test. Kept as short stable strings because they are written to the
# inference log and sliced on by the monitor.
VARIANT_COL = "variant"
ARM_CHAMPION = "A"
ARM_CHALLENGER = "B"

# Number of hash buckets. 100 makes ``split_pct`` a direct percentage.
BUCKETS = 100

# Default cohort salt. Changing it re-randomises which customers fall into arm B, which is
# a governed decision (it invalidates an in-flight experiment), so it is version-controlled
# here and overridable per environment through the ``ab_split_salt`` bundle variable.
DEFAULT_SALT = "credit_risk_ab_v1"


def variant_bucket(customer_id, salt: str = DEFAULT_SALT) -> int:
    """Map a customer to a stable bucket in ``[0, BUCKETS)``.

    Pure and deterministic: the same ``customer_id`` and ``salt`` always yield the same
    bucket, so a customer never flips arms between daily batches.
    """
    key = f"{salt}:{customer_id}".encode("utf-8")
    return zlib.crc32(key) % BUCKETS


def assign_variant(customer_id, split_pct: int, salt: str = DEFAULT_SALT) -> str:
    """Assign a customer to arm A (champion) or arm B (challenger).

    :param split_pct: Integer percentage of customers routed to arm B. ``0`` sends
        everyone to arm A; ``100`` sends everyone to arm B.
    """
    return ARM_CHALLENGER if variant_bucket(customer_id, salt) < split_pct else ARM_CHAMPION


def variant_bucket_expr(id_col: str = "customer_id", salt: str = DEFAULT_SALT):
    """Spark column expression mirroring :func:`variant_bucket`.

    Uses ``pmod`` rather than ``%`` so a negative CRC never yields a negative bucket, and
    concatenates the salt exactly as the Python function does so both agree bit-for-bit.
    """
    from pyspark.sql import functions as F

    key = F.concat(F.lit(f"{salt}:"), F.col(id_col).cast("string"))
    return F.pmod(F.crc32(key), F.lit(BUCKETS))


def variant_column_expr(
    id_col: str = "customer_id", split_pct: int = 0, salt: str = DEFAULT_SALT
):
    """Spark column expression mirroring :func:`assign_variant`.

    Returns a column of ``ARM_CHAMPION`` / ``ARM_CHALLENGER`` labels.
    """
    from pyspark.sql import functions as F

    return F.when(
        variant_bucket_expr(id_col, salt) < F.lit(int(split_pct)), F.lit(ARM_CHALLENGER)
    ).otherwise(F.lit(ARM_CHAMPION))
