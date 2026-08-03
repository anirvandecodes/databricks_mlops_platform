"""Promotion audit evidence.

Unity Catalog already tracks model lineage and alias changes in its system tables. That
covers "what happened", but a regulated credit-risk review also needs "on what basis" —
the metrics the approver saw, the exact training data version, and who signed off, all
captured at the moment of promotion.

This module writes that evidence package twice: as an immutable JSON file in a governed
UC Volume (the artifact an auditor is handed), and as a row in a Delta table (the surface
an analyst queries). Both are written before the alias moves, so an approval can never be
recorded without its supporting evidence.
"""
import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional


def build_evidence(
    model_name: str,
    version: str,
    approver: str,
    environment: str,
    eval_metrics: Dict[str, Any],
    training_data_version: Optional[str] = None,
    git_commit: Optional[str] = None,
    run_id: Optional[str] = None,
    decision: str = "APPROVED_AND_PROMOTED",
) -> Dict[str, Any]:
    """Assemble the evidence package recorded for a promotion decision.

    :param approver: Identity that authorised the promotion. For automated promotions
        this is the service principal, which keeps the audit trail honest about the fact
        that no human reviewed it.
    :param eval_metrics: Metrics the promotion decision was based on (AUC, PSI, ...).
    :param training_data_version: Delta version / timestamp of the training table, so the
        exact training input can be time-travelled to later.
    :param decision: Outcome, e.g. ``APPROVED_AND_PROMOTED`` or ``ROLLED_BACK``.
    """
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model_name": model_name,
        "promoted_version": str(version),
        "environment": environment,
        "approver": approver,
        "decision": decision,
        "evaluation_summary": eval_metrics,
        "training_data_version": training_data_version,
        "git_commit": git_commit,
        "mlflow_run_id": run_id,
    }


def write_evidence(
    spark,
    evidence: Dict[str, Any],
    volume_path: str,
    audit_table: str,
) -> str:
    """Persist an evidence package to the audit Volume and the audit table.

    The Volume write is the authoritative record; the table write is an index for
    querying. A failure to write the Volume file propagates — we would rather fail the
    promotion than promote a model with no auditable basis.

    :return: Path of the JSON evidence file that was written.
    """
    filename = (
        f"approval_{evidence['model_name'].replace('.', '_')}"
        f"_v{evidence['promoted_version']}"
        f"_{evidence['timestamp'].replace(':', '-')}.json"
    )
    file_path = f"{volume_path.rstrip('/')}/{filename}"

    payload = json.dumps(evidence, indent=2, default=str)
    # dbutils.fs.put would truncate at driver-side size limits; a plain write to the
    # /Volumes FUSE path is the supported way to land a file in a UC Volume.
    with open(file_path, "w") as handle:
        handle.write(payload)

    _append_audit_row(spark, evidence, file_path, audit_table)
    return file_path


def _append_audit_row(spark, evidence: Dict[str, Any], file_path: str, audit_table: str) -> None:
    """Append one queryable row summarising the promotion event."""
    from pyspark.sql import functions as F
    from pyspark.sql.types import StringType, StructField, StructType

    schema = StructType(
        [
            StructField("timestamp", StringType(), False),
            StructField("model_name", StringType(), False),
            StructField("promoted_version", StringType(), False),
            StructField("environment", StringType(), True),
            StructField("approver", StringType(), True),
            StructField("decision", StringType(), True),
            StructField("evaluation_summary", StringType(), True),
            StructField("training_data_version", StringType(), True),
            StructField("git_commit", StringType(), True),
            StructField("mlflow_run_id", StringType(), True),
            StructField("evidence_path", StringType(), True),
        ]
    )
    row = [
        (
            evidence["timestamp"],
            evidence["model_name"],
            str(evidence["promoted_version"]),
            evidence.get("environment"),
            evidence.get("approver"),
            evidence.get("decision"),
            # Metrics are stored as a JSON string so the audit table's schema stays
            # stable as models add or rename metrics over time.
            json.dumps(evidence.get("evaluation_summary", {}), default=str),
            evidence.get("training_data_version"),
            evidence.get("git_commit"),
            evidence.get("mlflow_run_id"),
            file_path,
        )
    ]
    (
        spark.createDataFrame(row, schema)
        .withColumn("recorded_at", F.current_timestamp())
        .write.format("delta")
        .mode("append")
        .option("mergeSchema", "true")
        .saveAsTable(audit_table)
    )
