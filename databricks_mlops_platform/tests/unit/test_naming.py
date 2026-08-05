"""Unit tests for asset addressing.

These lock in the property that environment separation is visible in every asset
address, which is what makes an accidental cross-environment write obvious in review.
"""
from platform_utils.naming import AssetNames


def _names(schema="mlops_dev"):
    return AssetNames(catalog="workspace", schema=schema)


def test_tables_are_three_level_qualified():
    names = _names()
    assert names.training_data == "workspace.mlops_dev.training_data"
    assert names.inference_log == "workspace.mlops_dev.inference_log"


def test_model_name_is_three_level():
    """UC model registry requires catalog.schema.model."""
    assert _names().model_name == "workspace.mlops_dev.credit_risk_model"


def test_model_uri_resolves_by_alias_not_version():
    """Alias indirection is what makes rollback a metadata operation."""
    assert _names().model_uri() == "models:/workspace.mlops_dev.credit_risk_model@champion"
    assert (
        _names().model_uri("challenger")
        == "models:/workspace.mlops_dev.credit_risk_model@challenger"
    )


def test_environments_never_collide():
    """The same code in different targets must not resolve to the same asset."""
    dev = _names("mlops_dev")
    prod = _names("mlops_prod")
    for attr in ("training_data", "inference_log", "model_name", "credit_decisions"):
        assert getattr(dev, attr) != getattr(prod, attr)


def test_monitor_metric_tables_match_data_profiling_convention():
    """Data profiling derives these names; we must not invent our own."""
    names = _names()
    assert names.profile_metrics == f"{names.inference_log}_profile_metrics"
    assert names.drift_metrics == f"{names.inference_log}_drift_metrics"


def test_audit_volume_path_uses_volumes_mount():
    names = _names()
    assert names.audit_volume_path == "/Volumes/workspace/mlops_dev/audit_logs"
    assert names.audit_volume == "workspace.mlops_dev.audit_logs"


def test_custom_model_name_is_honoured():
    names = AssetNames(catalog="workspace", schema="mlops_prod", model="fraud_model")
    assert names.model_name == "workspace.mlops_prod.fraud_model"


def test_layout_is_portable_to_catalog_per_environment():
    """Migrating to catalog-per-env is a variable change, not a code change."""
    shared = AssetNames(catalog="workspace", schema="mlops_prod")
    isolated = AssetNames(catalog="prod", schema="credit_risk")
    assert shared.training_data == "workspace.mlops_prod.training_data"
    assert isolated.training_data == "prod.credit_risk.training_data"
