"""Offline tests for the feature-contract rules (no workspace needed).

These pin the PR gate itself: what counts as a change to a feature table (contract + SQL),
which changes are never allowed, the version bump each needs, which dependencies a change
breaks, and the SQL and UC metadata derived from it. Each case starts from the released
contract and SQL on main, whatever their version, so the tests also pass on the PRs that
change them.
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
COMPILES = lambda query, sources: None  # noqa: E731


@pytest.fixture
def base():
    return fcu.load_yaml(CONTRACT, VARS), fcu.load_feature_sql(CONTRACT, VARS)


def report(*deps: dict, errors: tuple = ()) -> Report:
    return Report(TABLE, [Dependency(**d) for d in deps], [], list(errors))


# --- building PR variants of the released table -----------------------------------------

def _next(version: str, part: str) -> str:
    major, minor, patch = fcu.parse_version(version)
    return {"major": f"{major + 1}.0.0", "minor": f"{major}.{minor + 1}.0", "patch": f"{major}.{minor}.{patch + 1}"}[part]


def _bump(c: dict, part: str) -> dict:
    c["version"] = _next(c["version"], part)
    c.setdefault("changelog", []).append({"version": c["version"]})
    return c


def _feature(name: str, since: str, **kw) -> dict:
    return {"name": name, "dtype": "double", "definition": "x", "status": "active", "since": since, **kw}


def added(base):
    """Happy path: a new feature (contract entry + SQL file)."""
    contract, sql = copy.deepcopy(base[0]), dict(base[1])
    _bump(contract, "minor")
    contract["features"].append(_feature("new_test_feature", contract["version"]))
    sql["new_test_feature"] = "avg(t.amount)"
    return contract, sql


def changed_in_place(base):
    """Not-happy path: new SQL under a released feature's name (txn_count_30d)."""
    contract, sql = copy.deepcopy(base[0]), dict(base[1])
    _bump(contract, "major")
    sql["txn_count_30d"] = "count(CASE WHEN NOT t.is_refund THEN t.txn_id END)"
    return contract, sql


def right_way(base):
    """The fix: the new logic as a new feature, the old one deprecated."""
    contract, sql = copy.deepcopy(base[0]), dict(base[1])
    _bump(contract, "minor")
    contract["features"][0].update(status="deprecated", sunset_date="2099-12-31", replaced_by="replacement_feature")
    contract["features"].append(_feature("replacement_feature", contract["version"], dtype="int"))
    sql["replacement_feature"] = "count(CASE WHEN NOT t.is_refund THEN t.txn_id END)"
    return contract, sql


def removed_after_deprecation(base):
    """The released table with txn_count_30d deprecated, then a PR that removes it."""
    dep = copy.deepcopy(base[0])
    dep["features"][0].update(status="deprecated", sunset_date="2000-01-01", replaced_by="x")
    new = _bump(copy.deepcopy(dep), "major")
    new["features"].pop(0)
    sql = {k: v for k, v in base[1].items() if k != "txn_count_30d"}
    return (dep, base[1]), (new, sql)


def _diff(base, pr):
    return fcu.diff_contract(base[0], pr[0], base[1], pr[1])


def _kinds(base, pr):
    return {c["feature"]: c["kind"] for c in _diff(base, pr)}


def _problems(base, pr):
    return fcu.change_problems(base[0], pr[0], _diff(base, pr))


# --- the released table -----------------------------------------------------------------

def test_released_contract_and_sql_are_valid(base):
    assert fcu.validate_contract(base[0]) == [] and fcu.validate_sql(*base) == []


def test_validation_catches_missing_metadata(base):
    del base[0]["owner"]
    assert "missing field: owner" in fcu.validate_contract(base[0])


def test_deprecated_feature_needs_sunset_and_replacement(base):
    f = base[0]["features"][0]
    f["status"] = "deprecated"
    f.pop("sunset_date", None)
    f.pop("replaced_by", None)
    errors = fcu.validate_contract(base[0])
    assert any("sunset_date" in e for e in errors) and any("replaced_by" in e for e in errors)


def test_every_feature_needs_exactly_one_sql_file(base):
    contract, sql = base
    missing = {k: v for k, v in sql.items() if k != "txn_count_30d"}
    assert "feature txn_count_30d: no txn_count_30d.sql" in fcu.validate_sql(contract, missing)
    assert any("orphan.sql: not in the contract" in e for e in fcu.validate_sql(contract, {**sql, "orphan": "x"}))
    assert any("_base.sql" in e for e in fcu.validate_sql(contract, {k: v for k, v in sql.items() if k != "_base"}))


# --- what changed -------------------------------------------------------------------------

def test_new_feature_is_added(base):
    pr = added(base)
    assert _kinds(base, pr) == {"new_test_feature": "added"} and _problems(base, pr) == []


def test_sql_comments_and_whitespace_are_not_changes(base):
    sql = dict(base[1])
    sql["txn_count_30d"] = "-- reworded comment\n  " + sql["txn_count_30d"].replace(" ", "   ")
    assert _diff(base, (base[0], sql)) == []


def test_first_pr_adding_sql_has_nothing_to_compare(base):
    assert _diff((base[0], {}), base) == []


def test_definition_text_is_documentation(base):
    contract = _bump(copy.deepcopy(base[0]), "patch")
    contract["features"][0]["definition"] += " (clarified)"
    assert _kinds(base, (contract, base[1])) == {"txn_count_30d": "metadata"}
    assert _problems(base, (contract, base[1])) == []


def test_deprecation_is_allowed(base):
    pr = right_way(base)
    assert _kinds(base, pr) == {"txn_count_30d": "deprecated", "replacement_feature": "added"}
    assert _problems(base, pr) == []


# --- never allowed, whoever depends on the table -----------------------------------------

def test_sql_change_in_place_is_never_allowed(base):
    pr = changed_in_place(base)
    assert _kinds(base, pr) == {"txn_count_30d": "changed"}
    (problem,) = _problems(base, pr)
    assert "immutable" in problem and "txn_count_30d_v2" in problem


def test_dtype_change_in_place_is_never_allowed(base):
    contract = _bump(copy.deepcopy(base[0]), "major")
    contract["features"][0]["dtype"] = "double"
    assert any("immutable" in p for p in _problems(base, (contract, base[1])))


def test_active_feature_cannot_be_removed(base):
    contract = _bump(copy.deepcopy(base[0]), "major")
    contract["features"].pop(0)
    sql = {k: v for k, v in base[1].items() if k != "txn_count_30d"}
    assert any("deprecate it" in p for p in _problems(base, (contract, sql)))


def test_deprecated_feature_can_be_removed(base):
    dep, pr = removed_after_deprecation(base)
    assert _kinds(dep, pr) == {"txn_count_30d": "removed"} and _problems(dep, pr) == []


def test_removal_before_sunset_is_a_warning(base):
    dep, pr = removed_after_deprecation(base)
    dep[0]["features"][0]["sunset_date"] = "2099-01-01"
    (warning,) = fcu.change_warnings(dep[0], _diff(dep, pr), "2026-10-06")
    assert "before its promised sunset date 2099-01-01" in warning


@pytest.mark.parametrize("edit", ["keys", "query"])
def test_keys_and_query_skeleton_need_a_new_table(base, edit):
    contract, sql = _bump(copy.deepcopy(base[0]), "major"), dict(base[1])
    if edit == "keys":
        contract["primary_keys"] = ["account_id"]
    else:
        sql["_base"] = sql["_base"].replace("90)", "60)")
    assert any("new table" in p for p in _problems(base, (contract, sql)))


@pytest.mark.parametrize("make,too_small,label", [(added, "patch", "minor"), (right_way, "patch", "minor")])
def test_version_bump_must_match_change(base, make, too_small, label):
    contract, sql = make(base)
    contract["version"] = _next(base[0]["version"], too_small)
    contract["changelog"].append({"version": contract["version"]})
    assert any(f"{label} bump" in p for p in _problems(base, (contract, sql)))


def test_changelog_entry_required(base):
    contract = copy.deepcopy(base[0])
    contract["support_channel"] = "#team-a-help"
    contract["version"] = _next(base[0]["version"], "patch")
    assert _problems(base, (contract, base[1])) == [f"changelog has no entry for {contract['version']}"]


@pytest.mark.parametrize("name,expected", [("x", "x_v2"), ("x_v2", "x_v3"), ("txn_count_30d", "txn_count_30d_v2")])
def test_next_version_name(name, expected):
    assert fcu.next_version_name(name) == expected


# --- downstream breaks --------------------------------------------------------------------

def test_removal_breaks_its_readers(base):
    dep, pr = removed_after_deprecation(base)
    (b,) = fcu.downstream_breaks(_diff(dep, pr), [TEAM_B])
    assert b["feature"] == "txn_count_30d" and b["dependency"]["name"] == TEAM_B["name"]


def test_removal_of_an_unread_column_breaks_nobody(base):
    dep, pr = removed_after_deprecation(base)
    assert fcu.downstream_breaks(_diff(dep, pr), [{**TEAM_B, "features": ["txn_amount_sum_30d"]}]) == []


def test_unknown_columns_count_as_every_column(base):
    dep, pr = removed_after_deprecation(base)
    assert len(fcu.downstream_breaks(_diff(dep, pr), [{**TEAM_B, "features": None}])) == 1


# --- check_change.py end to end (the PR gate) ---------------------------------------------

def _write(tmp_path, table):
    """contracts/customer_features.yaml + features/customer_features/*.sql, as in the repo."""
    contract, sql = table
    path = tmp_path / "contracts" / "customer_features.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump(contract))
    (tmp_path / "features" / "customer_features").mkdir(parents=True)
    for name, text in sql.items():
        (tmp_path / "features" / "customer_features" / f"{name}.sql").write_text(text)
    return str(path)


def _run(tmp_path, base_table, pr_table, deps=lambda t: report(TEAM_B), compiles=COMPILES):
    base_path = _write(tmp_path / "base", base_table) if base_table is not None else str(CONTRACT)
    return check_change.run(base_path, _write(tmp_path / "pr", pr_table), VARS, deps, compiles)


def test_gate_happy_path_skips_the_dependency_lookup(tmp_path, base, capsys):
    looked_up = []
    assert _run(tmp_path, None, added(base), deps=lambda t: looked_up.append(t)) == 0
    assert looked_up == [] and "nothing that reads this table can break" in capsys.readouterr().out


def test_gate_blocks_change_in_place_and_shows_who_it_would_break(tmp_path, base, capsys):
    assert _run(tmp_path, None, changed_in_place(base)) == 1
    out = capsys.readouterr().out
    assert "immutable" in out and "x model demo.team_b_ml.default_risk_model" in out


def test_gate_blocks_change_in_place_even_with_no_readers(tmp_path, base):
    assert _run(tmp_path, None, changed_in_place(base), deps=lambda t: report()) == 1


def test_gate_right_way_passes(tmp_path, base):
    assert _run(tmp_path, None, right_way(base)) == 0


def test_gate_blocks_removal_while_something_reads_it(tmp_path, base, capsys):
    dep, pr = removed_after_deprecation(base)
    assert _run(tmp_path, dep, pr) == 1
    assert "this would break downstream" in capsys.readouterr().out


def test_gate_allows_removal_once_nothing_reads_it(tmp_path, base):
    dep, pr = removed_after_deprecation(base)
    assert _run(tmp_path, dep, pr, deps=lambda t: report()) == 0


def test_gate_blocks_sql_that_does_not_compile(tmp_path, base, capsys):
    assert _run(tmp_path, None, added(base), compiles=lambda q, s: "[UNRESOLVED_COLUMN] t.nope") == 1
    assert "doesn't compile" in capsys.readouterr().out


def test_gate_blocks_job_reading_removed_column(tmp_path, base, capsys):
    dep, pr = removed_after_deprecation(base)
    job = {**TEAM_B, "kind": "job", "name": "nightly-report", "detail": "latest read today", "source": "lineage"}
    assert _run(tmp_path, dep, pr, deps=lambda t: report(job)) == 1
    assert "x job nightly-report reads txn_count_30d" in capsys.readouterr().out


def test_gate_only_warns_on_ad_hoc_reads(tmp_path, base, capsys):
    dep, pr = removed_after_deprecation(base)
    adhoc = {**TEAM_B, "kind": "notebook", "name": "123", "detail": "latest read today", "blocking": False}
    assert _run(tmp_path, dep, pr, deps=lambda t: report(adhoc)) == 0
    assert "! notebook 123 read txn_count_30d" in capsys.readouterr().out


def test_gate_fails_closed_when_a_source_is_unreadable(tmp_path, base, capsys):
    dep, pr = removed_after_deprecation(base)
    assert _run(tmp_path, dep, pr, deps=lambda t: report(errors=("could not read column lineage: X",))) == 1
    assert "couldn't be fully checked" in capsys.readouterr().out


def test_gate_skips_environment_where_table_is_unpublished(tmp_path, base, capsys):
    dep, pr = removed_after_deprecation(base)
    assert _run(tmp_path, dep, pr, deps=lambda t: None) == 0
    assert "isn't published here yet" in capsys.readouterr().out


def test_gate_blocks_invalid_contract(tmp_path, base):
    contract, sql = added(base)
    del contract["owner"]
    assert _run(tmp_path, None, (contract, sql)) == 1


# --- SQL and UC metadata derived from the table -------------------------------------------

def test_features_sql_inserts_every_expression_outside_comments(base):
    query = fcu.features_sql(*added(base))
    assert "CAST(avg(t.amount) AS DOUBLE) AS new_test_feature" in query
    assert "${features}" not in query and "--" not in query


def test_schema_sync_adds_new_and_drops_removed(base):
    existing = ["customer_id", "as_of_date"] + fcu.feature_names(base[0])
    assert fcu.schema_sync_statements(added(base)[0], existing) == [
        f"ALTER TABLE {TABLE} ADD COLUMNS (new_test_feature DOUBLE)"]
    _, (removed, _) = removed_after_deprecation(base)
    assert fcu.schema_sync_statements(removed, existing) == [f"ALTER TABLE {TABLE} DROP COLUMN txn_count_30d"]


def test_metadata_statements_tag_status_and_sql_hash(base):
    contract, sql = right_way(base)
    stmts = fcu.uc_metadata_statements(contract, sql)
    dep = [s for s in stmts if "ALTER COLUMN txn_count_30d SET TAGS" in s][0]
    assert "'status' = 'deprecated'" in dep and "'replaced_by' = 'replacement_feature'" in dep
    assert f"'sql_hash' = '{fcu.expression_hash(sql['txn_count_30d'])}'" in dep
    assert any(f"'contract_version' = '{contract['version']}'" in s for s in stmts)


def test_sql_strings_are_escaped(base):
    base[0]["description"] = "Team A's features"
    assert "Team A\\'s features" in fcu.uc_metadata_statements(*base)[0]


# --- consumer side ------------------------------------------------------------------------

def test_lookup_specs_from_consumer_config():
    (spec,) = fcu.lookup_specs(fcu.load_yaml(CONSUMER_CFG, VARS))
    assert spec["table_name"] == TABLE and spec["lookup_key"] == "customer_id"
    assert spec["timestamp_lookup_key"] == "as_of_date" and spec["feature_names"]


def test_consumers_must_pin_features():
    with pytest.raises(ValueError, match="pin feature_names"):
        fcu.lookup_specs({"feature_dependencies": [{"table": "t", "lookup_key": "k", "feature_names": []}]})


def test_consumer_warned_about_deprecated_pins(base):
    contract, _ = right_way(base)
    (warning,) = fcu.deprecated_dependencies(fcu.load_yaml(CONSUMER_CFG, VARS), {TABLE: contract})
    assert "txn_count_30d" in warning and "migrate to replacement_feature" in warning


def test_feature_spec_columns_only_from_this_table():
    spec = {"input_columns": [
        {"customer_id": {"source": "training_data"}},
        {"txn_count_30d": {"table_name": TABLE, "feature_name": "txn_count_30d", "source": "feature_store"}},
        {"other": {"table_name": "demo.x.other_features", "feature_name": "other", "source": "feature_store"}},
    ]}
    assert dependencies.feature_spec_columns(spec, TABLE) == ["txn_count_30d"]
