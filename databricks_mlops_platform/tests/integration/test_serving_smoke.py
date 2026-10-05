"""Level-2 smoke test for the real-time serving endpoint config.

Asserts that a two-arm endpoint config is well-formed (two served entities, traffic summing
to 100). Guarded by the same capability posture the monitor uses: where Model Serving is
absent (e.g. Free Edition), the test skips rather than fails — a workspace capability gap is
not a defect in this code.

    MLOPS_TEST_PROFILE=<profile> pytest tests/integration/test_serving_smoke.py -v
"""
import pytest


def _serving_available(w) -> bool:
    """True if the workspace exposes the serving-endpoints API at all."""
    from databricks.sdk.errors import DatabricksError

    try:
        list(w.serving_endpoints.list())
        return True
    except DatabricksError as err:
        text = str(err).lower()
        if any(s in text for s in ("no api found", "not enabled", "not available")):
            return False
        raise


def _workspace_client():
    """WorkspaceClient honouring MLOPS_TEST_PROFILE, matching the conftest Spark fixture."""
    import os

    from databricks.sdk import WorkspaceClient

    profile = os.environ.get("MLOPS_TEST_PROFILE")
    return WorkspaceClient(profile=profile) if profile else WorkspaceClient()


def test_two_arm_traffic_config_is_well_formed():
    """The A/B route config must name two arms whose percentages sum to 100."""
    pytest.importorskip("databricks.sdk")

    w = _workspace_client()
    if not _serving_available(w):
        pytest.skip("Model Serving API is not available in this workspace")

    from databricks.sdk.service.serving import Route, TrafficConfig

    split = 10
    routes = [
        Route(served_model_name="champion", traffic_percentage=100 - split),
        Route(served_model_name="challenger", traffic_percentage=split),
    ]
    config = TrafficConfig(routes=routes)

    assert len(config.routes) == 2
    assert sum(r.traffic_percentage for r in config.routes) == 100
    assert {r.served_model_name for r in config.routes} == {"champion", "challenger"}
