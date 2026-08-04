"""Unit tests for the label backfill — no Spark, no cluster, fast enough for pre-commit.

The merge semantics are the whole point of this module, so they are asserted against the
generated SQL rather than against a live table: the guarantees below (idempotency, no
overwrite of settled labels, correct label cast) are decidable from the statement itself,
and pinning them here catches a regression without needing a warehouse.
"""
from monitoring.label_join import build_label_merge_sql


def test_merge_targets_the_log_and_reads_the_outcomes():
    sql = build_label_merge_sql("cat.sch.inference_log", "cat.sch.ground_truth_outcomes")
    assert "MERGE INTO cat.sch.inference_log" in sql
    assert "USING cat.sch.ground_truth_outcomes" in sql


def test_only_null_labels_are_updated():
    """The idempotency guarantee: without this predicate every run rewrites history."""
    sql = build_label_merge_sql("log", "gt")
    assert "WHEN MATCHED AND t.ground_truth IS NULL" in sql


def test_merge_never_inserts_unmatched_outcomes():
    """An outcome with no matching prediction must not create a phantom log row.

    The log is append-only evidence of what was actually served; inserting here would
    fabricate a prediction that never happened.
    """
    sql = build_label_merge_sql("log", "gt")
    assert "WHEN NOT MATCHED" not in sql
    assert "INSERT" not in sql.upper()


def test_merge_only_ever_updates_the_label_column():
    """A backfill that could rewrite features would corrupt the drift baseline."""
    sql = build_label_merge_sql("log", "gt")
    update_clause = sql.split("UPDATE SET", 1)[1]
    assert update_clause.count("=") == 1
    assert "t.ground_truth" in update_clause


def test_label_is_cast_to_double_to_match_the_monitor_schema():
    """The monitor pins label_col's type at creation; a bigint outcome must be cast."""
    sql = build_label_merge_sql("log", "gt")
    assert "CAST(s.is_default AS DOUBLE)" in sql


def test_join_key_and_column_names_are_configurable():
    sql = build_label_merge_sql(
        "log",
        "gt",
        key_col="application_id",
        label_col="observed",
        outcome_col="defaulted",
    )
    assert "ON t.application_id = s.application_id" in sql
    assert "t.observed IS NULL" in sql
    assert "CAST(s.defaulted AS DOUBLE)" in sql
