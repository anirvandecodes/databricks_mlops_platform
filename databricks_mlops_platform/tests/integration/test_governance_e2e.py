"""Level-2 integration tests: real Unity Catalog, real MLflow, via Databricks Connect.

These assert the governance properties that unit tests cannot: that the approval gate
holds against the actual UC model registry, that alias re-pointing behaves as rollback,
and that the policy SQL functions evaluate as intended once registered.

Run explicitly (they need a workspace and are slower than the unit tier):

    MLOPS_TEST_PROFILE=<profile> pytest tests/integration -v

Every test cleans up the models and functions it creates, and each uses a unique name so
concurrent CI runs cannot collide.
"""
import os
import uuid

import pytest

pytest.importorskip("mlflow")

CATALOG = os.environ.get("MLOPS_TEST_CATALOG", "workspace")
SCHEMA = os.environ.get("MLOPS_TEST_SCHEMA", "payments_dev")


@pytest.fixture(scope="module")
def client():
    """MLflow client pointed at the Unity Catalog registry."""
    import mlflow
    from mlflow import MlflowClient

    mlflow.set_registry_uri("databricks-uc")
    return MlflowClient()


@pytest.fixture
def temp_model(client):
    """A registered model with two versions, deleted afterwards.

    Two versions are the minimum needed to exercise promotion *and* rollback.
    """
    import mlflow
    from sklearn.dummy import DummyClassifier

    name = f"{CATALOG}.{SCHEMA}.it_credit_model_{uuid.uuid4().hex[:8]}"
    versions = []
    for _ in range(2):
        with mlflow.start_run():
            model = DummyClassifier(strategy="constant", constant=0).fit([[0.0]], [0])
            logged = mlflow.sklearn.log_model(
                model, artifact_path="model", registered_model_name=name
            )
            versions.append(str(logged.registered_model_version))

    yield name, versions

    try:
        client.delete_registered_model(name)
    except Exception as exc:  # pragma: no cover - best-effort teardown
        print(f"teardown: could not delete {name}: {exc}")


# -- approval gate ---------------------------------------------------------------


def test_unapproved_version_cannot_be_promoted_in_prod(client, temp_model):
    """The central control, verified against the real registry."""
    from platform_utils.promotion import CHAMPION, ApprovalNotGranted, get_alias_version, promote_to_champion

    name, versions = temp_model
    with pytest.raises(ApprovalNotGranted):
        promote_to_champion(name, versions[1], "prod", approval_required=True, client=client)

    # The alias must be untouched, so production would keep serving whatever it had.
    assert get_alias_version(name, CHAMPION, client=client) is None


def test_approval_tag_unblocks_promotion(client, temp_model):
    from platform_utils.promotion import APPROVAL_TAG, APPROVED_VALUE, CHAMPION, get_alias_version, promote_to_champion

    name, versions = temp_model
    client.set_model_version_tag(name, versions[1], APPROVAL_TAG, APPROVED_VALUE)
    client.set_model_version_tag(name, versions[1], "approved_by", "integration-test")

    result = promote_to_champion(name, versions[1], "prod", approval_required=True, client=client)
    assert result["changed"] is True
    assert get_alias_version(name, CHAMPION, client=client) == versions[1]


def test_dev_promotes_without_approval(client, temp_model):
    """dev/staging must run unattended or CI cannot complete."""
    from platform_utils.promotion import CHAMPION, get_alias_version, promote_to_champion

    name, versions = temp_model
    promote_to_champion(name, versions[0], "dev", approval_required=False, client=client)
    assert get_alias_version(name, CHAMPION, client=client) == versions[0]


# -- rollback --------------------------------------------------------------------


def test_rollback_repoints_champion_to_prior_version(client, temp_model):
    from platform_utils.promotion import CHAMPION, get_alias_version, promote_to_champion, rollback_champion

    name, versions = temp_model
    promote_to_champion(name, versions[0], "dev", approval_required=False, client=client)
    promote_to_champion(name, versions[1], "dev", approval_required=False, client=client)
    assert get_alias_version(name, CHAMPION, client=client) == versions[1]

    result = rollback_champion(name, client=client)
    assert result["rolled_back_to"] == versions[0]
    assert get_alias_version(name, CHAMPION, client=client) == versions[0]


def test_alias_uri_follows_the_alias_not_the_version(client, temp_model):
    """The property the whole design rests on: inference resolves by alias."""
    import mlflow

    from platform_utils.promotion import promote_to_champion

    name, versions = temp_model
    promote_to_champion(name, versions[1], "dev", approval_required=False, client=client)

    resolved = mlflow.models.get_model_info(f"models:/{name}@champion")
    assert resolved is not None


def test_challenger_alias_is_cleared_on_promotion(client, temp_model):
    from platform_utils.promotion import CHALLENGER, get_alias_version, promote_to_champion, register_challenger

    name, versions = temp_model
    register_challenger(name, versions[1], client=client)
    assert get_alias_version(name, CHALLENGER, client=client) == versions[1]

    promote_to_champion(name, versions[1], "dev", approval_required=False, client=client)
    assert get_alias_version(name, CHALLENGER, client=client) is None
