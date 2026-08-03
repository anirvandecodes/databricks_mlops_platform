"""Unit tests for the promotion gate and rollback.

The approval gate is the platform's most important control, so it is tested against a
fake registry rather than only in a live workspace: these assertions run on every commit
and would fail loudly if someone weakened the gate.
"""
import pytest

from platform_utils.promotion import (
    APPROVAL_TAG,
    CHAMPION,
    CHALLENGER,
    ApprovalNotGranted,
    get_alias_version,
    is_approved,
    promote_to_champion,
    register_challenger,
    rollback_champion,
)

MODEL = "workspace.mlops_prod.credit_risk_model"


class FakeModelVersion:
    def __init__(self, version, tags=None, aliases=None):
        self.version = str(version)
        self.tags = tags or {}
        self.aliases = aliases or []


class FakeClient:
    """Minimal stand-in for MlflowClient covering the alias/tag surface we use."""

    def __init__(self, versions=(1,), tags=None):
        self.versions = {str(v): FakeModelVersion(v, (tags or {}).get(str(v))) for v in versions}
        self.aliases = {}
        self.deleted_aliases = []

    def set_registered_model_alias(self, name, alias, version):
        self.aliases[alias] = str(version)

    def get_model_version_by_alias(self, name, alias):
        if alias not in self.aliases:
            raise Exception(f"alias {alias} not found")
        return self.versions[self.aliases[alias]]

    def get_model_version(self, name, version):
        return self.versions[str(version)]

    def delete_registered_model_alias(self, name, alias):
        self.deleted_aliases.append(alias)
        self.aliases.pop(alias, None)

    def search_model_versions(self, filter_string):
        return list(self.versions.values())


# -- approval gate ---------------------------------------------------------------


def test_unapproved_version_is_blocked_in_prod():
    """The core control: no approval tag means no promotion."""
    client = FakeClient(versions=(1, 2))
    with pytest.raises(ApprovalNotGranted, match="No 'approval_status' tag"):
        promote_to_champion(MODEL, "2", "prod", approval_required=True, client=client)
    # Critically, the alias must be untouched after a blocked promotion.
    assert CHAMPION not in client.aliases


def test_explicitly_rejected_version_is_blocked():
    client = FakeClient(versions=(1, 2), tags={"2": {APPROVAL_TAG: "rejected"}})
    with pytest.raises(ApprovalNotGranted, match="not approved"):
        promote_to_champion(MODEL, "2", "prod", approval_required=True, client=client)
    assert CHAMPION not in client.aliases


def test_approved_version_is_promoted():
    client = FakeClient(
        versions=(1, 2),
        tags={"2": {APPROVAL_TAG: "approved", "approved_by": "risk.reviewer"}},
    )
    result = promote_to_champion(MODEL, "2", "prod", approval_required=True, client=client)
    assert client.aliases[CHAMPION] == "2"
    assert result["changed"] is True
    assert "risk.reviewer" in result["detail"]


def test_approval_check_is_case_and_whitespace_tolerant():
    """Reviewers set this tag by hand; ' Approved ' must not silently fail the gate."""
    client = FakeClient(versions=(1,), tags={"1": {APPROVAL_TAG: "  Approved  "}})
    approved, _ = is_approved(MODEL, "1", client=client)
    assert approved is True


def test_gate_is_skipped_when_not_required():
    """dev/staging run unattended, so the pipeline must not need a human."""
    client = FakeClient(versions=(1,))
    result = promote_to_champion(MODEL, "1", "dev", approval_required=False, client=client)
    assert client.aliases[CHAMPION] == "1"
    assert "Approval gate disabled" in result["detail"]


# -- promotion mechanics ---------------------------------------------------------


def test_promotion_reports_displaced_version():
    """The previous champion is what a rollback needs to target."""
    client = FakeClient(versions=(1, 2))
    client.aliases[CHAMPION] = "1"
    result = promote_to_champion(MODEL, "2", "dev", approval_required=False, client=client)
    assert result["previous_champion"] == "1"
    assert client.aliases[CHAMPION] == "2"


def test_promotion_clears_challenger_alias():
    """A serving model must not still be labelled an untested candidate."""
    client = FakeClient(versions=(1, 2))
    register_challenger(MODEL, "2", client=client)
    assert client.aliases[CHALLENGER] == "2"
    promote_to_champion(MODEL, "2", "dev", approval_required=False, client=client)
    assert CHALLENGER in client.deleted_aliases


def test_repromoting_current_champion_is_a_noop():
    client = FakeClient(versions=(1,))
    client.aliases[CHAMPION] = "1"
    result = promote_to_champion(MODEL, "1", "dev", approval_required=False, client=client)
    assert result["changed"] is False


# -- rollback --------------------------------------------------------------------


def test_rollback_to_explicit_version():
    client = FakeClient(versions=(1, 2, 3))
    client.aliases[CHAMPION] = "3"
    result = rollback_champion(MODEL, target_version="1", client=client)
    assert client.aliases[CHAMPION] == "1"
    assert result["previous_champion"] == "3"


def test_rollback_defaults_to_preceding_version():
    """The incident case: undo the last promotion without naming a version."""
    client = FakeClient(versions=(1, 2, 3))
    client.aliases[CHAMPION] = "3"
    result = rollback_champion(MODEL, client=client)
    assert result["rolled_back_to"] == "2"
    assert client.aliases[CHAMPION] == "2"


def test_rollback_fails_when_no_earlier_version_exists():
    client = FakeClient(versions=(1,))
    client.aliases[CHAMPION] = "1"
    with pytest.raises(ValueError, match="No prior version"):
        rollback_champion(MODEL, client=client)


def test_rollback_does_not_require_approval():
    """Rollback is a safety valve; gating it would prolong an incident."""
    client = FakeClient(versions=(1, 2))
    client.aliases[CHAMPION] = "2"
    result = rollback_champion(MODEL, client=client)
    assert result["changed"] is True


def test_missing_alias_reads_as_none():
    assert get_alias_version(MODEL, CHAMPION, client=FakeClient()) is None
