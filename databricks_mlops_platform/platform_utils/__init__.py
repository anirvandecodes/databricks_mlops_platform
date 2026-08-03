"""Shared platform layer for the MLOps reference platform.

Deliberately separated from project ML code. Utilities here are environment-aware,
governance-related plumbing that every ML project needs and no data scientist should
rewrite: asset naming, drift metrics, promotion/rollback, and audit evidence.

Project-specific logic (feature transforms, model choice, business policy) lives in the
sibling packages so it stays readable and locally debuggable — the platform is a library,
not an opaque engine wheel.
"""
from platform_utils.audit import build_evidence, write_evidence
from platform_utils.metrics import population_stability_index, psi_verdict
from platform_utils.naming import AssetNames, from_widgets
from platform_utils.promotion import (
    CHAMPION,
    CHALLENGER,
    ApprovalNotGranted,
    get_alias_version,
    is_approved,
    promote_to_champion,
    register_challenger,
    rollback_champion,
)

__all__ = [
    "AssetNames",
    "from_widgets",
    "population_stability_index",
    "psi_verdict",
    "build_evidence",
    "write_evidence",
    "CHAMPION",
    "CHALLENGER",
    "ApprovalNotGranted",
    "promote_to_champion",
    "register_challenger",
    "rollback_champion",
    "get_alias_version",
    "is_approved",
]
