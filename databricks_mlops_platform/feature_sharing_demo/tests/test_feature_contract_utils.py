"""Offline tests for the feature-contract rules (no workspace needed).

These pin the review process itself: what counts as additive vs breaking, which edits are
blocked outright, who must approve, and the UC DDL derived from a contract. They run on
the real demo contracts in team_a_producer/changes/ so the demo cannot drift from the rules.
"""

import copy
import sys
from pathlib import Path

import pytest

DEMO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DEMO / "shared"))
sys.path.insert(0, str(DEMO / "governance"))

import check_change  # noqa: E402
import feature_contract_utils as fcu  # noqa: E402

CHANGES = DEMO / "team_a_producer" / "changes"
CRS = DEMO / "governance" / "change_requests"
VARS = {"catalog": "demo", "raw_schema": "raw", "producer_schema": "team_a_features", "consumer_schema": "team_b_ml"}


def contract(version: str) -> dict:
    return fcu.load_yaml(CHANGES / f"{version}.yaml", VARS)


@pytest.fixture
def v1():
    return contract("v1.0.0")


def approved(cr_id: str, teams: list[str]) -> dict:
    cr = fcu.load_yaml(CRS / f"{cr_id}.yaml", VARS)
    cr["approvals"] = [{"team": t, "decision": "approved"} for t in teams]
    return cr


# --- contracts in the repo are valid --------------------------------------------------

@pytest.mark.parametrize("version", ["v1.0.0", "v1.1.0", "v1.1.0_wrong_in_place", "v2.0.0"])
def test_demo_contracts_are_valid(version):
    assert fcu.validate_contract(contract(version)) == []


def test_released_contract_matches_a_known_version():
    released = fcu.load_yaml(DEMO / "team_a_producer" / "contracts" / "customer_features.yaml", VARS)
    assert released["version"] in {"1.0.0", "1.1.0", "2.0.0"}


def test_validation_catches_missing_metadata(v1):
    del v1["owner"]
    assert "missing field: owner" in fcu.validate_contract(v1)


def test_deprecated_feature_needs_sunset_and_replacement(v1):
    v1["features"][0]["status"] = "deprecated"
    errors = fcu.validate_contract(v1)
    assert any("sunset_date" in e for e in errors) and any("replaced_by" in e for e in errors)


# --- classification: the demo's three releases ---------------------------------------

def test_in_place_logic_change_is_blocked(v1):
    result = fcu.classify_change(v1, contract("v1.1.0_wrong_in_place"))
    assert result["kind"] == "breaking"
    assert any("txn_count_30d_v2" in v for v in result["violations"])


def test_v2_column_plus_deprecation_is_allowed_but_breaking(v1):
    result = fcu.classify_change(v1, contract("v1.1.0"))
    assert result["violations"] == []
    assert result["kind"] == "breaking"  # deprecation needs consumer sign-off
    kinds = {c["feature"]: c["kind"] for c in result["changes"]}
    assert kinds == {"txn_count_30d": "breaking", "txn_count_30d_v2": "additive", "avg_txn_amount_90d": "additive"}


def test_removing_deprecated_feature_needs_major_bump():
    result = fcu.classify_change(contract("v1.1.0"), contract("v2.0.0"))
    assert result["violations"] == []
    bad = contract("v2.0.0")
    bad["version"] = "1.2.0"
    bad["changelog"].append({"version": "1.2.0"})
    assert any("major bump" in v for v in fcu.classify_change(contract("v1.1.0"), bad)["violations"])


# --- classification: individual rules -------------------------------------------------

def _bump(c: dict, version: str) -> dict:
    c["version"] = version
    c.setdefault("changelog", []).append({"version": version})
    return c


def test_additive_only(v1):
    new = _bump(copy.deepcopy(v1), "1.1.0")
    new["features"].append({"name": "new_feat", "dtype": "double", "definition": "x", "logic_version": 1,
                            "status": "active", "since": "1.1.0"})
    result = fcu.classify_change(v1, new)
    assert result["kind"] == "additive" and result["violations"] == []


def test_dtype_change_blocked(v1):
    new = _bump(copy.deepcopy(v1), "1.1.0")
    new["features"][0]["dtype"] = "double"
    assert any("dtype changed in place" in v for v in fcu.classify_change(v1, new)["violations"])


def test_removing_active_feature_blocked(v1):
    new = _bump(copy.deepcopy(v1), "2.0.0")
    new["features"].pop(0)
    assert any("deprecate it first" in v for v in fcu.classify_change(v1, new)["violations"])


def test_key_change_requires_new_table(v1):
    new = _bump(copy.deepcopy(v1), "2.0.0")
    new["primary_keys"] = ["account_id"]
    assert any("new table" in v for v in fcu.classify_change(v1, new)["violations"])


def test_missing_version_bump_blocked(v1):
    new = copy.deepcopy(v1)
    new["features"].append({"name": "new_feat", "dtype": "int", "definition": "x", "logic_version": 1,
                            "status": "active", "since": "1.0.0"})
    assert any("minor bump" in v for v in fcu.classify_change(v1, new)["violations"])


def test_metadata_only_change_is_patch(v1):
    new = _bump(copy.deepcopy(v1), "1.0.1")
    new["support_channel"] = "#team-a-help"
    result = fcu.classify_change(v1, new)
    assert result["kind"] == "metadata" and result["violations"] == []


def test_whitespace_in_definition_is_not_a_logic_change(v1):
    new = copy.deepcopy(v1)
    new["features"][0]["definition"] = "  " + new["features"][0]["definition"].replace(" ", "   ")
    assert fcu.classify_change(v1, new)["kind"] == "none"


@pytest.mark.parametrize("name,expected", [("x", "x_v2"), ("x_v2", "x_v3"), ("txn_count_30d", "txn_count_30d_v2")])
def test_next_version_name(name, expected):
    assert fcu.next_version_name(name) == expected


# --- approvals ------------------------------------------------------------------------

def test_breaking_change_needs_producer_and_consumers(v1):
    result = fcu.classify_change(v1, contract("v1.1.0"))
    required = fcu.required_approvers(result, contract("v1.1.0"), ["team_b", "team_c"])
    assert required == ["team_a", "team_b", "team_c"]
    assert fcu.missing_approvals(required, approved("CR-001", ["team_a"])) == ["team_b", "team_c"]


def test_additive_change_needs_producer_only(v1):
    new = _bump(copy.deepcopy(v1), "1.1.0")
    new["features"].append({"name": "f", "dtype": "int", "definition": "x", "logic_version": 1,
                            "status": "active", "since": "1.1.0"})
    assert fcu.required_approvers(fcu.classify_change(v1, new), new, ["team_b"]) == ["team_a"]


def test_rejected_approval_does_not_count():
    cr = {"approvals": [{"team": "team_b", "decision": "changes_requested"}]}
    assert fcu.missing_approvals(["team_b"], cr) == ["team_b"]


# --- check_change.py end to end (the CI gate) -----------------------------------------

def _write(tmp_path, name, data):
    import yaml
    p = tmp_path / name
    p.write_text(yaml.safe_dump(data))
    return str(p)


def test_gate_blocks_wrong_way(capsys):
    rc = check_change.run(str(CHANGES / "v1.0.0.yaml"), str(CHANGES / "v1.1.0_wrong_in_place.yaml"),
                          str(CRS / "CR-001.yaml"))
    assert rc == 1 and "logic changed in place" in capsys.readouterr().out


def test_gate_blocks_until_all_approve(tmp_path):
    base, proposed = str(CHANGES / "v1.0.0.yaml"), str(CHANGES / "v1.1.0.yaml")
    assert check_change.run(base, proposed, _write(tmp_path, "cr_a.yaml", approved("CR-001", ["team_a"]))) == 1
    assert check_change.run(base, proposed, _write(tmp_path, "cr_ab.yaml", approved("CR-001", ["team_a", "team_b"]))) == 0


def test_gate_removal_with_no_consumers_needs_producer_only(tmp_path):
    cr = _write(tmp_path, "cr2.yaml", approved("CR-002", ["team_a"]))
    assert check_change.run(str(CHANGES / "v1.1.0.yaml"), str(CHANGES / "v2.0.0.yaml"), cr) == 0


# --- UC DDL derived from the contract -------------------------------------------------

def test_schema_sync_adds_new_and_drops_removed():
    v11 = contract("v1.1.0")
    existing = ["customer_id", "as_of_date", "txn_count_30d", "txn_amount_sum_30d", "days_since_last_txn"]
    assert fcu.schema_sync_statements(v11, existing) == [
        "ALTER TABLE demo.team_a_features.customer_features ADD COLUMNS "
        "(txn_count_30d_v2 INT, avg_txn_amount_90d DOUBLE)"]
    v2 = contract("v2.0.0")
    stmts = fcu.schema_sync_statements(v2, existing + ["txn_count_30d_v2", "avg_txn_amount_90d"])
    assert stmts == ["ALTER TABLE demo.team_a_features.customer_features DROP COLUMN txn_count_30d"]


def test_metadata_statements_tag_deprecated_columns():
    stmts = fcu.uc_metadata_statements(contract("v1.1.0"))
    dep = [s for s in stmts if "ALTER COLUMN txn_count_30d SET TAGS" in s][0]
    assert "'status' = 'deprecated'" in dep and "'replaced_by' = 'txn_count_30d_v2'" in dep
    assert any("'contract_version' = '1.1.0'" in s for s in stmts)


def test_sql_strings_are_escaped(v1):
    v1["description"] = "Team A's features"
    assert "Team A\\'s features" in fcu.uc_metadata_statements(v1)[0]


# --- consumer side --------------------------------------------------------------------

def test_lookup_specs_from_consumer_config():
    cfg = fcu.load_yaml(DEMO / "team_b_consumer" / "changes" / "feature_config.v1.yaml", VARS)
    (spec,) = fcu.lookup_specs(cfg)
    assert spec == {"table_name": "demo.team_a_features.customer_features",
                    "feature_names": ["txn_count_30d", "txn_amount_sum_30d", "days_since_last_txn"],
                    "lookup_key": "customer_id", "timestamp_lookup_key": "as_of_date"}


def test_consumers_must_pin_features():
    with pytest.raises(ValueError, match="pin feature_names"):
        fcu.lookup_specs({"feature_dependencies": [{"table": "t", "lookup_key": "k", "feature_names": []}]})


def test_consumer_warned_about_deprecated_pins():
    v1_cfg = fcu.load_yaml(DEMO / "team_b_consumer" / "changes" / "feature_config.v1.yaml", VARS)
    v2_cfg = fcu.load_yaml(DEMO / "team_b_consumer" / "changes" / "feature_config.v2.yaml", VARS)
    contracts = {"demo.team_a_features.customer_features": contract("v1.1.0")}
    (warning,) = fcu.deprecated_dependencies(v1_cfg, contracts)
    assert "txn_count_30d" in warning and "migrate to txn_count_30d_v2" in warning
    assert fcu.deprecated_dependencies(v2_cfg, contracts) == []
