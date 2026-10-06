"""Offline tests for the feature-contract rules (no workspace needed).

These pin the PR gate itself: what counts as a change, which edits are never allowed, the
version bump each change needs, which live consumers a change breaks, and the UC DDL
derived from a contract. Each case starts from the contract in team_a_producer/contracts/,
whatever its version, so the tests keep passing on the PRs that change it.
"""

import copy
import sys
from pathlib import Path

import pytest
import yaml

DEMO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DEMO / "shared"))
sys.path.insert(0, str(DEMO / "governance"))

import check_change  # noqa: E402
import dependencies  # noqa: E402
import feature_contract_utils as fcu  # noqa: E402
from dependencies import Dependency, Report  # noqa: E402

CONTRACT = DEMO / "team_a_producer" / "contracts" / "customer_features.yaml"
CONSUMER_CFG = DEMO / "team_b_consumer" / "feature_config.yaml"
VARS = {"catalog": "demo", "raw_schema": "raw", "producer_schema": "team_a_features", "consumer_schema": "team_b_ml"}
TABLE = "demo.team_a_features.customer_features"
TEAM_B = {"kind": "model", "name": "demo.team_b_ml.default_risk_model", "detail": "v1 @champion (team_b)",
          "features": ["txn_count_30d", "txn_amount_sum_30d", "days_since_last_txn"], "source": "feature spec",
          "blocking": True}


def report(*deps: dict, errors: tuple = ()) -> Report:
    return Report(TABLE, [Dependency(**d) for d in deps], [], list(errors))


@pytest.fixture
def base():
    return fcu.load_yaml(CONTRACT, VARS)


def _next(version: str, part: str) -> str:
    major, minor, patch = fcu.parse_version(version)
    return {"major": f"{major + 1}.0.0", "minor": f"{major}.{minor + 1}.0", "patch": f"{major}.{minor}.{patch + 1}"}[part]


def _bump(c: dict, part: str) -> dict:
    c["version"] = _next(c["version"], part)
    c.setdefault("changelog", []).append({"version": c["version"]})
    return c


def _feature(name: str, since: str, **kw) -> dict:
    return {"name": name, "dtype": "double", "definition": "x", "logic_version": 1,
            "status": "active", "since": since, **kw}


def added(base: dict) -> dict:
    """Happy path: a new column."""
    new = _bump(copy.deepcopy(base), "minor")
    new["features"].append(_feature("new_test_feature", new["version"]))
    return new


def changed_in_place(base: dict) -> dict:
    """Not-happy path: new logic under an existing name (txn_count_30d)."""
    new = _bump(copy.deepcopy(base), "major")
    new["features"][0]["definition"] += " Now computed differently."
    new["features"][0]["logic_version"] += 1
    return new


def _kinds(base: dict, new: dict) -> dict:
    return {c["feature"]: c["kind"] for c in fcu.diff_contract(base, new)}


# --- the released contract ------------------------------------------------------------

def test_released_contract_is_valid(base):
    assert fcu.validate_contract(base) == []


def test_validation_catches_missing_metadata(base):
    del base["owner"]
    assert "missing field: owner" in fcu.validate_contract(base)


def test_deprecated_feature_needs_sunset_and_replacement(base):
    base["features"][0]["status"] = "deprecated"
    base["features"][0].pop("sunset_date", None)
    base["features"][0].pop("replaced_by", None)
    errors = fcu.validate_contract(base)
    assert any("sunset_date" in e for e in errors) and any("replaced_by" in e for e in errors)


# --- what changed ---------------------------------------------------------------------

def test_new_feature_is_added(base):
    new = added(base)
    assert _kinds(base, new) == {"new_test_feature": "added"}
    assert fcu.change_problems(base, new, fcu.diff_contract(base, new)) == []


def test_logic_change_in_place_is_changed(base):
    new = changed_in_place(base)
    assert _kinds(base, new) == {"txn_count_30d": "changed"}
    assert fcu.change_problems(base, new, fcu.diff_contract(base, new)) == []


def test_removal_is_removed(base):
    new = _bump(copy.deepcopy(base), "major")
    new["features"].pop(0)
    assert _kinds(base, new) == {"txn_count_30d": "removed"}


def test_deprecation_is_not_breaking(base):
    base["features"][0]["status"] = "active"
    new = _bump(copy.deepcopy(base), "minor")
    new["features"][0].update(status="deprecated", sunset_date="2026-12-31", replaced_by="replacement_test_feature")
    new["features"].append(_feature("replacement_test_feature", new["version"], dtype="int"))
    assert _kinds(base, new) == {"txn_count_30d": "deprecated", "replacement_test_feature": "added"}
    assert fcu.downstream_breaks(fcu.diff_contract(base, new), [TEAM_B]) == []


def test_whitespace_in_definition_is_not_a_change(base):
    new = copy.deepcopy(base)
    new["features"][0]["definition"] = "  " + new["features"][0]["definition"].replace(" ", "   ")
    assert fcu.diff_contract(base, new) == []


# --- never allowed, whoever the consumers are -----------------------------------------

def test_dtype_change_in_place_not_allowed(base):
    new = _bump(copy.deepcopy(base), "major")
    new["features"][0]["dtype"] = "double"
    problems = fcu.change_problems(base, new, fcu.diff_contract(base, new))
    assert any("publish txn_count_30d_v2 instead" in p for p in problems)


def test_key_change_needs_new_table(base):
    new = _bump(copy.deepcopy(base), "major")
    new["primary_keys"] = ["account_id"]
    assert any("new table" in p for p in fcu.change_problems(base, new, fcu.diff_contract(base, new)))


@pytest.mark.parametrize("make,too_small,label", [
    (added, "patch", "minor"),             # new feature
    (changed_in_place, "minor", "major"),  # breaking change
])
def test_version_bump_must_match_change(base, make, too_small, label):
    new = make(base)
    new["version"] = _next(base["version"], too_small)
    new["changelog"].append({"version": new["version"]})
    assert any(f"{label} bump" in p for p in fcu.change_problems(base, new, fcu.diff_contract(base, new)))


def test_metadata_change_needs_patch_and_changelog(base):
    new = copy.deepcopy(base)
    new["support_channel"] = "#team-a-help"
    new["version"] = _next(base["version"], "patch")
    assert fcu.change_problems(base, new, fcu.diff_contract(base, new)) == [
        f"changelog has no entry for {new['version']}"]


@pytest.mark.parametrize("name,expected", [("x", "x_v2"), ("x_v2", "x_v3"), ("txn_count_30d", "txn_count_30d_v2")])
def test_next_version_name(name, expected):
    assert fcu.next_version_name(name) == expected


# --- downstream breaks ----------------------------------------------------------------

def test_change_to_a_read_column_breaks_its_reader(base):
    (b,) = fcu.downstream_breaks(fcu.diff_contract(base, changed_in_place(base)), [TEAM_B])
    assert b["feature"] == "txn_count_30d" and b["dependency"]["name"] == TEAM_B["name"]


def test_change_to_an_unread_column_breaks_nobody(base):
    reads_other = {**TEAM_B, "features": ["txn_amount_sum_30d"]}
    assert fcu.downstream_breaks(fcu.diff_contract(base, changed_in_place(base)), [reads_other]) == []


def test_unknown_columns_count_as_every_column(base):
    unknown = {**TEAM_B, "features": None}
    assert len(fcu.downstream_breaks(fcu.diff_contract(base, changed_in_place(base)), [unknown])) == 1


def test_key_change_breaks_every_reader(base):
    new = copy.deepcopy(base)
    new["timestamp_key"] = "snapshot_date"
    reads_other = {**TEAM_B, "features": ["txn_amount_sum_30d"]}
    assert len(fcu.downstream_breaks(fcu.diff_contract(base, new), [TEAM_B, reads_other])) == 2


# --- check_change.py end to end (the PR gate) -----------------------------------------

def _write(tmp_path, name, data):
    p = tmp_path / name
    p.write_text(yaml.safe_dump(data))
    return str(p)


def test_gate_happy_path(tmp_path, base, capsys):
    rc = check_change.run(str(CONTRACT), _write(tmp_path, "pr.yaml", added(base)), VARS, lambda t: report(TEAM_B))
    assert rc == 0 and "nothing breaks" in capsys.readouterr().out


def test_gate_blocks_change_that_breaks_downstream(tmp_path, base, capsys):
    seen = []
    rc = check_change.run(str(CONTRACT), _write(tmp_path, "pr.yaml", changed_in_place(base)), VARS,
                          lambda t: seen.append(t) or report(TEAM_B))
    out = capsys.readouterr().out
    assert rc == 1 and seen == [TABLE]
    assert "this would break downstream" in out and "txn_count_30d_v2" in out


def test_gate_allows_breaking_change_with_no_readers(tmp_path, base):
    assert check_change.run(str(CONTRACT), _write(tmp_path, "pr.yaml", changed_in_place(base)), VARS,
                            lambda t: report()) == 0


def test_gate_blocks_job_reading_changed_column(tmp_path, base, capsys):
    job = {**TEAM_B, "kind": "job", "name": "nightly-report", "detail": "latest read today", "source": "lineage"}
    rc = check_change.run(str(CONTRACT), _write(tmp_path, "pr.yaml", changed_in_place(base)), VARS,
                          lambda t: report(job))
    assert rc == 1 and "x job nightly-report reads txn_count_30d" in capsys.readouterr().out


def test_gate_only_warns_on_ad_hoc_reads(tmp_path, base, capsys):
    adhoc = {**TEAM_B, "kind": "notebook", "name": "123", "detail": "latest read today", "blocking": False}
    rc = check_change.run(str(CONTRACT), _write(tmp_path, "pr.yaml", changed_in_place(base)), VARS,
                          lambda t: report(adhoc))
    assert rc == 0 and "! notebook 123 read txn_count_30d" in capsys.readouterr().out


def test_gate_fails_closed_when_a_source_is_unreadable(tmp_path, base, capsys):
    rc = check_change.run(str(CONTRACT), _write(tmp_path, "pr.yaml", added(base)), VARS,
                          lambda t: report(errors=("could not read column lineage: PermissionDenied",)))
    assert rc == 1 and "couldn't be fully checked" in capsys.readouterr().out


def test_gate_skips_environment_where_table_is_unpublished(tmp_path, base, capsys):
    rc = check_change.run(str(CONTRACT), _write(tmp_path, "pr.yaml", changed_in_place(base)), VARS, lambda t: None)
    assert rc == 0 and "isn't published here yet" in capsys.readouterr().out


def test_gate_blocks_invalid_contract(tmp_path, base):
    del base["owner"]
    assert check_change.run(str(CONTRACT), _write(tmp_path, "pr.yaml", base), VARS, lambda t: report()) == 1


# --- UC DDL derived from the contract -------------------------------------------------

def test_schema_sync_adds_new_and_drops_removed(base):
    existing = ["customer_id", "as_of_date"] + fcu.feature_names(base)
    assert fcu.schema_sync_statements(added(base), existing) == [
        f"ALTER TABLE {TABLE} ADD COLUMNS (new_test_feature DOUBLE)"]
    removed = copy.deepcopy(base)
    removed["features"].pop(0)
    assert fcu.schema_sync_statements(removed, existing) == [f"ALTER TABLE {TABLE} DROP COLUMN txn_count_30d"]


def test_metadata_statements_tag_deprecated_columns(base):
    base["features"][0].update(status="deprecated", sunset_date="2026-12-31", replaced_by="txn_count_30d_v2")
    stmts = fcu.uc_metadata_statements(base)
    dep = [s for s in stmts if "ALTER COLUMN txn_count_30d SET TAGS" in s][0]
    assert "'status' = 'deprecated'" in dep and "'replaced_by' = 'txn_count_30d_v2'" in dep
    assert any(f"'contract_version' = '{base['version']}'" in s for s in stmts)


def test_sql_strings_are_escaped(base):
    base["description"] = "Team A's features"
    assert "Team A\\'s features" in fcu.uc_metadata_statements(base)[0]


# --- consumer side --------------------------------------------------------------------

def test_lookup_specs_from_consumer_config():
    (spec,) = fcu.lookup_specs(fcu.load_yaml(CONSUMER_CFG, VARS))
    assert spec["table_name"] == TABLE and spec["lookup_key"] == "customer_id"
    assert spec["timestamp_lookup_key"] == "as_of_date" and spec["feature_names"]


def test_consumers_must_pin_features():
    with pytest.raises(ValueError, match="pin feature_names"):
        fcu.lookup_specs({"feature_dependencies": [{"table": "t", "lookup_key": "k", "feature_names": []}]})


def test_consumer_warned_about_deprecated_pins(base):
    base["features"][0].update(status="deprecated", sunset_date="2026-12-31", replaced_by="txn_count_30d_v2")
    cfg = fcu.load_yaml(CONSUMER_CFG, VARS)
    (warning,) = fcu.deprecated_dependencies(cfg, {TABLE: base})
    assert "txn_count_30d" in warning and "migrate to txn_count_30d_v2" in warning


def test_feature_spec_columns_only_from_this_table():
    spec = {"input_columns": [
        {"customer_id": {"source": "training_data"}},
        {"txn_count_30d": {"table_name": TABLE, "feature_name": "txn_count_30d", "source": "feature_store"}},
        {"other": {"table_name": "demo.x.other_features", "feature_name": "other", "source": "feature_store"}},
    ]}
    assert dependencies.feature_spec_columns(spec, TABLE) == ["txn_count_30d"]
