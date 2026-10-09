"""Offline tests for the feature-contract rules (no workspace needed).

These pin the PR gate itself: what counts as a change to a feature table (contract + SQL),
which changes are never allowed, the version bump each needs, which dependencies a change
breaks, and the SQL and UC metadata derived from it. Each case starts from the released
contract and SQL on main, whatever their version, so the tests also pass on the PRs that
change them.
"""

import copy
import re
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

CONTRACT = DEMO / "features" / "customer_features" / "customer_features.yaml"
ENV = "staging"
TABLE = "workspace.team_a_features_staging.customer_features"
# A consumer's declared dependency on the table (inline — the demo's consumer just reads the
# table and shows lineage, so there is no feature_config.yaml file to load).
CONSUMER_CFG = {"feature_dependencies": [
    {"table": TABLE, "contract_version": "1.0.0", "lookup_key": "customer_id",
     "timestamp_lookup_key": "as_of_date",
     "feature_names": ["txn_count", "txn_amount_sum", "days_since_last_txn"]}]}
TEAM_B = {"kind": "model", "name": "demo.team_b_ml.default_risk_model", "detail": "v1 @champion (team_b)",
          "features": ["txn_count", "txn_amount_sum", "days_since_last_txn"], "source": "feature spec",
          "blocking": True}
COMPILES = lambda query, sources: None  # noqa: E731


@pytest.fixture
def base():
    return fcu.load_table(CONTRACT, ENV)


def active(base):
    """The released table with txn_count active — some cases start from that, whatever
    the checked-in contract says (a PR may already have deprecated it)."""
    contract = copy.deepcopy(base[0])
    contract["features"][0]["status"] = "active"
    contract["features"][0].pop("sunset_date", None)
    contract["features"][0].pop("replaced_by", None)
    return contract, base[1]


def report(*deps: dict, errors: tuple = ()) -> Report:
    return Report(TABLE, [Dependency(**d) for d in deps], [], list(errors))


# --- writing a flat contract back as the ODCS file a team commits -----------------------

def _props(items: dict) -> list[dict]:
    return [{"property": k, "value": v} for k, v in items.items() if v is not None]


def _odcs(c: dict) -> dict:
    """The ODCS v3.0.1 file for a flat contract: the inverse of fcu.from_odcs, so a test can
    edit the flat dict and write what a team would commit. Fields the flat dict lacks are
    left out (for the gate to catch)."""
    doc = {"apiVersion": "v3.0.1", "kind": "DataContract", "id": c.get("id") or "test-id",
           "status": "active", "version": c.get("version")}
    if "name" in c:
        doc["name"] = c["name"]
    if "description" in c:
        doc["description"] = {"purpose": c["description"]}
    if "owner" in c:
        doc["team"] = [{"username": c["owner"], "role": "owner"}]
    if "support_channel" in c:
        doc["support"] = [{"channel": c["support_channel"], "url": "https://slack.com/app_redirect"}]
    if isinstance(c.get("environments"), dict):
        doc["servers"] = [{"server": env, "type": "databricks", "environment": env,
                           **{k: e[k] for k in ("catalog", "schema") if k in e},
                           "customProperties": _props({"sources": e.get("sources")})}
                          for env, e in c["environments"].items()]
    props = [{"name": k, "physicalType": "bigint", "primaryKey": True, "primaryKeyPosition": i}
             for i, k in enumerate(c.get("primary_keys", []), 1)]
    if "timestamp_key" in c:
        props.append({"name": c["timestamp_key"], "physicalType": "date",
                      "customProperties": _props({"timestampKey": True})})
    for f in c.get("features", []):
        props.append({"name": f["name"], **({"physicalType": f["dtype"]} if "dtype" in f else {}),
                      **({"description": f["definition"]} if "definition" in f else {}),
                      "customProperties": _props({"status": f.get("status"), "since": f.get("since"),
                                                  "sunsetDate": f.get("sunset_date"),
                                                  "replacedBy": f.get("replaced_by")})})
    doc["schema"] = [{"name": c.get("name", "unnamed"), "physicalType": "table", "properties": props}]
    sla = []
    if "refresh" in c:
        value, unit = re.match(r"^(\d+)(\w*)$", str(c["refresh"])).groups()
        sla.append({"property": "frequency", "value": int(value), "unit": unit})
    if "freshness_sla_hours" in c:
        sla.append({"property": "latency", "value": c["freshness_sla_hours"], "unit": "h"})
    if sla:
        doc["slaProperties"] = sla
    if "access_list" in c:
        doc["roles"] = [{"role": e["principal"], "access": "write" if "MODIFY" in e["privileges"] else "read"}
                        for e in c["access_list"]]
    doc["customProperties"] = _props({"online": c.get("online"), "changelog": c.get("changelog")})
    return doc


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
    """Not-happy path: new SQL under a released feature's name (txn_count).

    Derive the edited SQL from whatever txn_count currently is, so this is always a real
    in-place change regardless of what the checked-in txn_count.sql happens to contain (a
    demo branch may already have rewritten it)."""
    contract, sql = copy.deepcopy(base[0]), dict(base[1])
    _bump(contract, "major")
    sql["txn_count"] = base[1]["txn_count"].rstrip() + " + 0"
    return contract, sql


def right_way(base):
    """The fix: the new logic as a new feature, the old one deprecated (compare with active(base))."""
    contract, sql = copy.deepcopy(active(base)[0]), dict(base[1])
    _bump(contract, "minor")
    contract["features"][0].update(status="deprecated", sunset_date="2099-12-31", replaced_by="replacement_feature")
    contract["features"].append(_feature("replacement_feature", contract["version"], dtype="int"))
    sql["replacement_feature"] = "count(CASE WHEN NOT t.is_refund THEN t.txn_id END)"
    return contract, sql


def removed_after_deprecation(base):
    """The released table with txn_count deprecated, then a PR that removes it."""
    dep = copy.deepcopy(base[0])
    dep["features"][0].update(status="deprecated", sunset_date="2000-01-01", replaced_by="x")
    new = _bump(copy.deepcopy(dep), "major")
    new["features"].pop(0)
    sql = {k: v for k, v in base[1].items() if k != "txn_count"}
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
    assert f"missing field: {fcu.ODCS_FIELDS['owner']}" in fcu.validate_contract(base[0])


def test_deprecated_feature_needs_sunset_and_replacement(base):
    f = base[0]["features"][0]
    f["status"] = "deprecated"
    f.pop("sunset_date", None)
    f.pop("replaced_by", None)
    errors = fcu.validate_contract(base[0])
    assert any("sunsetDate" in e for e in errors) and any("replacedBy" in e for e in errors)


def test_every_feature_needs_exactly_one_sql_file(base):
    contract, sql = base
    missing = {k: v for k, v in sql.items() if k != "txn_count"}
    assert "feature txn_count: no txn_count.sql" in fcu.validate_sql(contract, missing)
    assert any("orphan.sql: not in the contract" in e for e in fcu.validate_sql(contract, {**sql, "orphan": "x"}))
    assert any("_base.sql" in e for e in fcu.validate_sql(contract, {k: v for k, v in sql.items() if k != "_base"}))


# --- what changed -------------------------------------------------------------------------

def test_new_feature_is_added(base):
    pr = added(base)
    assert _kinds(base, pr) == {"new_test_feature": "added"} and _problems(base, pr) == []


def test_sql_comments_and_whitespace_are_not_changes(base):
    sql = dict(base[1])
    sql["txn_count"] = "-- reworded comment\n  " + sql["txn_count"].replace(" ", "   ")
    assert _diff(base, (base[0], sql)) == []


def test_first_pr_adding_sql_has_nothing_to_compare(base):
    assert _diff((base[0], {}), base) == []


def test_definition_text_is_documentation(base):
    contract = _bump(copy.deepcopy(base[0]), "patch")
    contract["features"][0]["definition"] += " (clarified)"
    assert _kinds(base, (contract, base[1])) == {"txn_count": "metadata"}
    assert _problems(base, (contract, base[1])) == []


def test_deprecation_is_allowed(base):
    pr = right_way(base)
    assert _kinds(active(base), pr) == {"txn_count": "deprecated", "replacement_feature": "added"}
    assert _problems(active(base), pr) == []


# --- never allowed, whoever depends on the table -----------------------------------------

def test_sql_change_in_place_is_never_allowed(base):
    pr = changed_in_place(base)
    assert _kinds(base, pr) == {"txn_count": "changed"}
    (problem,) = _problems(base, pr)
    assert "immutable" in problem and "txn_count_v2" in problem


def test_dtype_change_in_place_is_never_allowed(base):
    contract = _bump(copy.deepcopy(base[0]), "major")
    contract["features"][0]["dtype"] = "double"
    assert any("immutable" in p for p in _problems(base, (contract, base[1])))


def test_active_feature_cannot_be_removed(base):
    base = active(base)
    contract = _bump(copy.deepcopy(base[0]), "major")
    contract["features"].pop(0)
    sql = {k: v for k, v in base[1].items() if k != "txn_count"}
    assert any("deprecate it" in p for p in _problems(base, (contract, sql)))


def test_deprecated_feature_can_be_removed(base):
    dep, pr = removed_after_deprecation(base)
    assert _kinds(dep, pr) == {"txn_count": "removed"} and _problems(dep, pr) == []


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
        sql["_base"] = sql["_base"].replace("current_date()", "DATE'2026-09-30'")
    assert any("new table" in p for p in _problems(base, (contract, sql)))


@pytest.mark.parametrize("make,too_small,label", [(added, "patch", "minor"), (right_way, "patch", "minor")])
def test_version_bump_must_match_change(base, make, too_small, label):
    contract, sql = make(base)
    contract["version"] = _next(active(base)[0]["version"], too_small)
    contract["changelog"].append({"version": contract["version"]})
    assert any(f"{label} bump" in p for p in _problems(active(base), (contract, sql)))


def test_changelog_entry_required(base):
    contract = copy.deepcopy(base[0])
    contract["support_channel"] = "#team-a-help"
    contract["version"] = _next(base[0]["version"], "patch")
    assert _problems(base, (contract, base[1])) == [f"changelog has no entry for {contract['version']}"]


@pytest.mark.parametrize("name,expected", [("x", "x_v2"), ("x_v2", "x_v3"), ("txn_count", "txn_count_v2")])
def test_next_version_name(name, expected):
    assert fcu.next_version_name(name) == expected


# --- downstream breaks --------------------------------------------------------------------

def test_removal_breaks_its_readers(base):
    dep, pr = removed_after_deprecation(base)
    (b,) = fcu.downstream_breaks(_diff(dep, pr), [TEAM_B])
    assert b["feature"] == "txn_count" and b["dependency"]["name"] == TEAM_B["name"]


def test_removal_of_an_unread_column_breaks_nobody(base):
    dep, pr = removed_after_deprecation(base)
    assert fcu.downstream_breaks(_diff(dep, pr), [{**TEAM_B, "features": ["txn_amount_sum"]}]) == []


def test_unknown_columns_count_as_every_column(base):
    dep, pr = removed_after_deprecation(base)
    assert len(fcu.downstream_breaks(_diff(dep, pr), [{**TEAM_B, "features": None}])) == 1


# --- check_change.py end to end (the PR gate) ---------------------------------------------

def _write(tmp_path, table):
    """features/customer_features/ with the contract.yaml (as ODCS) beside its *.sql, as in
    the repo."""
    contract, sql = table
    folder = tmp_path / "customer_features"
    folder.mkdir(parents=True)
    path = folder / "customer_features.yaml"
    path.write_text(yaml.safe_dump(_odcs(contract)))
    for name, text in sql.items():
        (folder / f"{name}.sql").write_text(text)
    return str(path)


def _run(tmp_path, base_table, pr_table, deps=lambda t: report(TEAM_B), compiles=COMPILES):
    base_path = _write(tmp_path / "base", base_table) if base_table is not None else str(CONTRACT)
    return check_change.run(base_path, _write(tmp_path / "pr", pr_table), ENV, deps, compiles)


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
    assert _run(tmp_path, active(base), right_way(base)) == 0


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
    assert "x job nightly-report reads txn_count" in capsys.readouterr().out


def test_gate_only_warns_on_ad_hoc_reads(tmp_path, base, capsys):
    dep, pr = removed_after_deprecation(base)
    adhoc = {**TEAM_B, "kind": "notebook", "name": "123", "detail": "latest read today", "blocking": False}
    assert _run(tmp_path, dep, pr, deps=lambda t: report(adhoc)) == 0
    assert "! notebook 123 read txn_count" in capsys.readouterr().out


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


# --- ODCS v3.0.1 -------------------------------------------------------------------------

def test_contract_file_is_odcs_compliant():
    assert fcu.odcs_errors(CONTRACT) == []


def test_test_writer_is_the_inverse_of_from_odcs():
    flat = fcu.load_yaml(CONTRACT)
    assert fcu.from_odcs(_odcs(flat)) == flat


def test_gate_blocks_a_contract_that_isnt_odcs(tmp_path, base, capsys):
    contract, sql = added(base)
    path = Path(_write(tmp_path / "pr", (contract, sql)))
    path.write_text(path.read_text() + "access_list: [team_b]\n")  # not an ODCS field
    assert check_change.run(None, str(path), ENV, None, COMPILES) == 1
    assert "ODCS <root>: Additional properties are not allowed ('access_list' was unexpected)" in capsys.readouterr().out


def test_gate_blocks_non_odcs_fields_in_a_feature(tmp_path, base, capsys):
    path = Path(_write(tmp_path / "pr", added(base)))
    doc = yaml.safe_load(path.read_text())
    doc["schema"][0]["properties"][-1]["dtype"] = "double"
    path.write_text(yaml.safe_dump(doc))
    assert check_change.run(None, str(path), ENV, None, COMPILES) == 1
    assert "'dtype' was unexpected" in capsys.readouterr().out




def test_a_contract_that_isnt_odcs_is_rejected(tmp_path, base, capsys):
    """The flat format from before ODCS (environments:, access_list:, features:)."""
    flat = {k: v for k, v in base[0].items() if k not in ("table", "sources", "environment", "problems")}
    assert any("must be an ODCS v3.0.1 data contract" in e for e in fcu.validate_contract(fcu.from_odcs(flat)))
    path = Path(_write(tmp_path / "pr", base))
    path.write_text(yaml.safe_dump(flat))
    assert check_change.run(None, str(path), ENV, None, COMPILES) == 1
    assert "'apiVersion' is a required property" in capsys.readouterr().out


def test_contract_ids_must_be_unique(tmp_path, base, capsys):
    other = _new_table(base)
    other[0]["id"] = base[0]["id"]
    assert _run_all(tmp_path, [], [("customer_features", base), ("other_features", other)]) == 1
    assert "used by more than one contract" in capsys.readouterr().out


@pytest.mark.parametrize("edit, error", [
    (lambda d: d["team"].append({"username": "team_z", "role": "owner"}), "one member with role owner"),
    (lambda d: d["servers"].append(dict(d["servers"][0])), "more than one dev server"),
    (lambda d: d["slaProperties"].__setitem__(1, {"property": "latency", "value": 3, "unit": "w"}),
     "slaProperties latency"),
    (lambda d: d.pop("servers"), "missing field: servers"),
])
def test_odcs_structure_problems(edit, error):
    doc = yaml.safe_load(CONTRACT.read_text())
    edit(doc)
    assert any(error in e for e in fcu.validate_contract(fcu.from_odcs(doc)))


@pytest.mark.parametrize("edit", [
    lambda d: d.__setitem__("servers", ["dev"]),
    lambda d: d.__setitem__("team", ["team_a"]),
    lambda d: d.__setitem__("support", {"channel": "x"}),
    lambda d: d.__setitem__("schema", {"name": "t"}),
    lambda d: d["schema"][0].__setitem__("properties", ["txn_count"]),
    lambda d: d.__setitem__("roles", "everyone"),
    lambda d: d.__setitem__("slaProperties", [7]),
], ids=["servers", "team", "support", "schema", "properties", "roles", "sla"])
def test_malformed_contract_is_blocked_not_a_crash(tmp_path, base, capsys, edit):
    path = Path(_write(tmp_path / "pr" / "features", base))
    doc = yaml.safe_load(path.read_text())
    edit(doc)
    path.write_text(yaml.safe_dump(doc))
    fcu.from_odcs(doc)  # never raises
    assert check_change.run(None, str(path), ENV, None, COMPILES) == 1
    assert "BLOCKED — invalid contract" in capsys.readouterr().out
    assert check_all.run_all(tmp_path / "nothing", tmp_path / "pr", ENV, None, COMPILES) == 1


def test_invalid_yaml_is_blocked_not_a_crash(tmp_path, base, capsys):
    path = Path(_write(tmp_path / "pr" / "features", base))
    path.write_text("schema: [unclosed\n")
    assert check_change.run(None, str(path), ENV, None, COMPILES) == 1
    assert "isn't valid YAML" in capsys.readouterr().out
    assert check_all.run_all(tmp_path / "nothing", tmp_path / "pr", ENV, None, COMPILES) == 1


def test_a_table_needs_at_least_one_feature():
    doc = yaml.safe_load(CONTRACT.read_text())
    doc["schema"][0]["properties"] = [p for p in doc["schema"][0]["properties"]
                                      if p.get("primaryKey") or p["name"] == "as_of_date"]
    assert f"missing field: {fcu.ODCS_FIELDS['features']}" in fcu.validate_contract(fcu.from_odcs(doc))


def test_contract_id_never_changes(base):
    contract = _bump(copy.deepcopy(base[0]), "patch")
    contract["id"] = "a-new-id"
    contract["support_channel"] = "#elsewhere"
    assert any("contract id changed" in p for p in _problems(base, (contract, base[1])))


def test_online_and_status_changes_are_metadata(base):
    contract = _bump(copy.deepcopy(base[0]), "patch")
    contract.update(online=not base[0].get("online"), status="deprecated")
    changes = fcu.diff_contract(base[0], contract)
    assert {c["message"] for c in changes} == {"online changed", "status changed"}
    assert fcu.change_problems(base[0], contract, changes) == []


def test_primary_keys_follow_their_position_then_file_order():
    doc = {"apiVersion": "v3.0.1", "schema": [{"name": "t", "properties": [
        {"name": "region", "primaryKey": True},
        {"name": "day", "primaryKey": True, "primaryKeyPosition": 2},
        {"name": "customer_id", "primaryKey": True, "primaryKeyPosition": 1},
        {"name": "channel", "primaryKey": True}]}]}
    assert fcu.from_odcs(doc)["primary_keys"] == ["customer_id", "day", "region", "channel"]


def test_odcs_validator_is_built_once():
    assert fcu._odcs_validator() is fcu._odcs_validator()


@pytest.mark.parametrize("edit, error", [
    (lambda d: d["servers"].append({"server": "files", "type": "s3", "location": "not a url"}), "'not a url' is not a 'uri'"),
    (lambda d: d["team"][0].__setitem__("dateIn", "last week"), "'last week' is not a 'date'"),
    (lambda d: d.__setitem__("contractCreatedTs", "yesterday"), "is not a 'date-time'"),
    (lambda d: d.__setitem__("contractCreatedTs", "2026-10-09"), "is not a 'date-time'"),
])
def test_odcs_formats_are_checked(edit, error):
    doc = yaml.safe_load(CONTRACT.read_text())
    edit(doc)
    assert any(error in e for e in fcu.odcs_errors(doc))


def test_valid_odcs_formats_pass():
    doc = yaml.safe_load(CONTRACT.read_text())
    doc["contractCreatedTs"] = "2026-10-09T04:00:00Z"
    doc["servers"].append({"server": "files", "type": "s3", "location": "s3://bucket/features/"})
    assert fcu.odcs_errors(doc) == []


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
    assert fcu.schema_sync_statements(removed, existing) == [f"ALTER TABLE {TABLE} DROP COLUMN txn_count"]


def test_metadata_statements_tag_status_and_sql_hash(base):
    contract, sql = right_way(base)
    stmts = fcu.uc_metadata_statements(contract, sql)
    dep = [s for s in stmts if "ALTER COLUMN txn_count SET TAGS" in s][0]
    assert "'status' = 'deprecated'" in dep and "'replaced_by' = 'replacement_feature'" in dep
    assert f"'sql_hash' = '{fcu.expression_hash(sql['txn_count'])}'" in dep
    assert any(f"'contract_version' = '{contract['version']}'" in s for s in stmts)


def test_sql_strings_are_escaped(base):
    base[0]["description"] = "Team A's features"
    assert "Team A\\'s features" in fcu.uc_metadata_statements(*base)[0]


# --- roles: who can read the table ----------------------------------------------------------

def _with_access(contract: dict, roles: list | str, part: str | None = None) -> dict:
    """The contract with these ODCS roles (a name means {role: name, access: read}), plus any
    problems from_odcs found in them."""
    c = copy.deepcopy(contract)
    roles = roles if not isinstance(roles, list) else [{"role": r, "access": "read"} if isinstance(r, str) else r
                                                       for r in roles]
    flat = fcu.from_odcs({"apiVersion": "v3.0.1", "roles": roles})
    c["access_list"] = flat["access_list"]
    c["problems"] = c.get("problems", []) + [p for p in flat["problems"] if p.startswith("roles")]
    return _bump(c, part) if part else c


def test_read_gets_select_and_write_gets_modify(base):
    c = _with_access(base[0], ["team_b", {"role": "team_a_eng", "access": "write"}])
    assert fcu.validate_contract(c) == []
    assert fcu.access_grants(c) == {("team_b", "SELECT"), ("team_a_eng", "SELECT"), ("team_a_eng", "MODIFY")}


@pytest.mark.parametrize("access, error", [
    ([{"role": "team_b", "access": "drop"}], "access must be one of read, write, not 'drop'"),
    ([{"role": "team_b", "access": "modify"}], "access must be one of read, write, not 'modify'"),
    ([{"role": "team_b", "access": "SELECT"}], "access must be one of read, write"),
    ([{"role": "team_b"}], "team_b: needs access (read, write)"),
    (["team_b", "team_b"], "listed twice"),
    ([{"access": "read"}], "needs a role"),
    ("team_b", "must be a list"),
    (["team_b", 7], "7 must be {role, access}"),
])
def test_access_list_validation(base, access, error):
    assert any(error in e for e in fcu.validate_contract(_with_access(base[0], access)))


def test_grant_statements_match_the_list_exactly(base):
    c = _with_access(base[0], ["account users", "team_b"])
    current = {("team_b", "SELECT"), ("team_c", "SELECT"), ("team_a", "MODIFY"), ("me@x.com", "SELECT")}
    stmts = fcu.grant_statements(c, current, keep={"me@x.com"})
    assert f"GRANT SELECT ON TABLE {TABLE} TO `account users`" in stmts
    assert f"GRANT USE SCHEMA ON SCHEMA workspace.team_a_features_staging TO `account users`" in stmts
    assert f"GRANT USE CATALOG ON CATALOG workspace TO `team_b`" in stmts
    assert not any(s.startswith(f"GRANT SELECT ON TABLE {TABLE} TO `team_b`") for s in stmts)  # already has it
    assert f"REVOKE SELECT ON TABLE {TABLE} FROM `team_c`" in stmts
    assert f"REVOKE MODIFY ON TABLE {TABLE} FROM `team_a`" in stmts
    assert not any("me@x.com" in s for s in stmts)


def test_no_access_list_means_grants_are_not_managed(base):
    c = copy.deepcopy(base[0])
    c.pop("access_list", None)
    assert fcu.grant_statements(c, {("team_c", "SELECT")}, keep=set()) == []


def test_granting_access_is_a_patch(base):
    old = _with_access(base[0], ["team_b"])
    new = _with_access(old, ["team_b", "team_c"], "patch")
    changes = fcu.diff_contract(old, new)
    assert [(c["kind"], c["message"]) for c in changes] == [("metadata", "team_c granted SELECT")]
    assert fcu.change_problems(old, new, changes) == []


def test_revoking_access_is_a_minor_bump_with_a_warning(base):
    old = _with_access(base[0], ["team_b", "team_c"])
    too_small = _with_access(old, ["team_b"], "patch")
    assert any("needs a minor bump" in p for p in fcu.change_problems(old, too_small, fcu.diff_contract(old, too_small)))
    new = _with_access(old, ["team_b"], "minor")
    changes = fcu.diff_contract(old, new)
    assert [c["kind"] for c in changes] == ["revoked"] and fcu.change_problems(old, new, changes) == []
    assert any("team_c loses SELECT" in w for w in fcu.change_warnings(old, changes, "2026-01-01"))


# --- check_all.py: every table in the repo, whichever team owns it ------------------------

import check_all  # noqa: E402


def _repo(root, *tables):
    """A repo checkout at root with features/<folder>/ for each (folder, (contract, sql))."""
    for folder, table in tables:
        contract, sql = table
        d = root / "features" / folder
        d.mkdir(parents=True)
        (d / f"{folder}.yaml").write_text(yaml.safe_dump(_odcs(contract)))
        for name, text in sql.items():
            (d / f"{name}.sql").write_text(text)
    (root / "features").mkdir(parents=True, exist_ok=True)
    return root


def _new_table(base, name="other_features"):
    contract, sql = copy.deepcopy(base[0]), dict(base[1])
    contract.update(name=name, id=f"{name}-id")
    return contract, sql


def _run_all(tmp_path, base_tables, pr_tables, exists=lambda t: None, compiles=COMPILES):
    return check_all.run_all(_repo(tmp_path / "base", *base_tables), _repo(tmp_path / "pr", *pr_tables),
                             ENV, exists, compiles)


def test_every_table_is_checked_and_a_new_table_passes(tmp_path, base, capsys):
    assert _run_all(tmp_path, [("customer_features", base)],
                    [("customer_features", base), ("other_features", _new_table(base))]) == 0
    out = capsys.readouterr().out
    assert "(new table" in out and "2 feature table(s) checked" in out


def test_a_bad_change_in_one_table_blocks_the_pr(tmp_path, base):
    assert _run_all(tmp_path, [("customer_features", base)],
                    [("customer_features", changed_in_place(base)), ("other_features", _new_table(base))],
                    exists=lambda t: report()) == 1


def test_new_table_sql_must_compile(tmp_path, base, capsys):
    assert _run_all(tmp_path, [], [("other_features", _new_table(base))],
                    compiles=lambda q, s: "UNRESOLVED_COLUMN") == 1
    assert "doesn't compile" in capsys.readouterr().out


def test_new_table_cannot_take_an_existing_name(tmp_path, base, capsys):
    assert _run_all(tmp_path, [], [("other_features", _new_table(base))], exists=lambda t: report()) == 1
    assert "already exists in Unity Catalog" in capsys.readouterr().out


def test_new_table_needs_a_changelog_entry(tmp_path, base):
    contract, sql = _new_table(base)
    contract["changelog"] = []
    assert _run_all(tmp_path, [], [("other_features", (contract, sql))]) == 1


def test_deleting_a_table_folder_is_blocked(tmp_path, base, capsys):
    assert _run_all(tmp_path, [("customer_features", base)], []) == 1
    assert "was deleted" in capsys.readouterr().out


def test_two_contracts_cannot_declare_one_table(tmp_path, base, capsys):
    assert _run_all(tmp_path, [("customer_features", base)],
                    [("customer_features", base), ("copy_features", base)]) == 1
    assert "declared by more than one contract" in capsys.readouterr().out


# --- environments: where each table lives ------------------------------------------------

def _raw():
    return fcu.load_yaml(CONTRACT)


def test_each_environment_has_its_own_table_and_sources():
    dev, dev_sql = fcu.load_table(CONTRACT, "dev")
    prod, _ = fcu.load_table(CONTRACT, "prod")
    assert dev["table"] != prod["table"] and dev["sources"] != prod["sources"]
    assert dev["sources"][0] in dev_sql["_base"] and "${transactions}" not in dev_sql["_base"]
    assert dev["table"] == "{catalog}.{schema}.customer_features".format(**dev["environments"]["dev"])


def test_every_environment_must_be_defined(base):
    contract = copy.deepcopy(base[0])
    del contract["environments"]["prod"]
    assert any("prod missing" in e for e in fcu.validate_contract(contract))


def test_environment_needs_catalog_schema_and_full_source_names(base):
    contract = copy.deepcopy(base[0])
    contract["environments"]["dev"].pop("schema")
    contract["environments"]["prod"]["sources"] = {"transactions": "just_a_table"}
    errors = fcu.validate_contract(contract)
    assert any("dev: needs a schema" in e for e in errors)
    assert any("must be catalog.schema.table" in e for e in errors)


def test_source_names_must_match_across_environments(base):
    contract = copy.deepcopy(base[0])
    contract["environments"]["prod"]["sources"] = {"txns": "a.b.c"}
    assert any("same names" in e for e in fcu.validate_contract(contract))


def test_unfilled_placeholder_is_an_error(tmp_path, base):
    contract, sql = base
    sql = {**sql, "txn_count": "count(${typo})"}
    path = _write(tmp_path, (contract, sql))
    with pytest.raises(ValueError, match=r"\$\{typo\} not defined"):
        fcu.load_table(path, ENV)


def test_gate_blocks_unfilled_placeholder(tmp_path, base, capsys):
    contract, sql = added(base)
    sql["new_test_feature"] = "avg(${typo}.amount)"
    assert _run(tmp_path, None, (contract, sql)) == 1
    assert "${typo} not defined" in capsys.readouterr().out


def test_moving_a_table_in_any_environment_needs_a_new_table(base):
    contract = _bump(copy.deepcopy(base[0]), "major")
    contract["environments"]["prod"]["schema"] = "somewhere_else"
    kinds = {c["message"]: c["kind"] for c in fcu.diff_contract(base[0], contract)}
    assert any(k.startswith("prod: table moves") and v == "keys" for k, v in kinds.items())
    assert any("new table" in p for p in _problems(base, (contract, base[1])))


def test_repointing_a_source_is_a_patch(base):
    contract = _bump(copy.deepcopy(base[0]), "patch")
    contract["environments"]["dev"]["sources"] = {"transactions": "other.raw.transactions"}
    changes = fcu.diff_contract(base[0], contract)
    assert [(c["kind"], c["message"]) for c in changes] == [("metadata", "dev sources changed")]
    assert fcu.change_problems(base[0], contract, changes) == []




def test_two_contracts_cannot_share_a_table_in_any_environment(tmp_path, base, capsys):
    """Different tables in dev and staging, but both claim the same prod table."""
    other = copy.deepcopy(base[0])
    for env in ("dev", "staging"):
        other["environments"][env]["schema"] = "team_c_features"
    assert _run_all(tmp_path, [], [("customer_features", base), ("team_c_copy", (other, base[1]))]) == 1
    assert f"{base[0]['environments']['prod']['catalog']}.{base[0]['environments']['prod']['schema']}" \
           f".customer_features (prod) is declared by more than one contract" in capsys.readouterr().out


# --- consumer side ------------------------------------------------------------------------

def test_lookup_specs_from_consumer_config():
    (spec,) = fcu.lookup_specs(CONSUMER_CFG)
    assert spec["table_name"] == TABLE and spec["lookup_key"] == "customer_id"
    assert spec["timestamp_lookup_key"] == "as_of_date" and spec["feature_names"]


def test_consumers_must_pin_features():
    with pytest.raises(ValueError, match="pin feature_names"):
        fcu.lookup_specs({"feature_dependencies": [{"table": "t", "lookup_key": "k", "feature_names": []}]})


def test_consumer_warned_about_deprecated_pins(base):
    contract, _ = right_way(base)
    (warning,) = fcu.deprecated_dependencies(CONSUMER_CFG, {TABLE: contract})
    assert "txn_count" in warning and "migrate to replacement_feature" in warning


def test_feature_spec_columns_only_from_this_table():
    spec = {"input_columns": [
        {"customer_id": {"source": "training_data"}},
        {"txn_count": {"table_name": TABLE, "feature_name": "txn_count", "source": "feature_store"}},
        {"other": {"table_name": "demo.x.other_features", "feature_name": "other", "source": "feature_store"}},
    ]}
    assert dependencies.feature_spec_columns(spec, TABLE) == ["txn_count"]
