# Databricks notebook source
# Feature-contract utilities shared by producers, consumers and the governance checks.
# Usage from notebooks:  %run ../../shared/feature_contract_utils
# Usage from tests/CI:   import feature_contract_utils
#
# Plain functions only — no top-level spark/dbutils — so the same logic runs in the
# workspace (%run), in pytest, and in the PR gate (governance/check_change.py).

# COMMAND ----------

import re
from pathlib import Path
from typing import Any

import yaml

REQUIRED_CONTRACT_FIELDS = (
    "table", "version", "owner", "support_channel", "description",
    "primary_keys", "timestamp_key", "refresh", "freshness_sla_hours", "sources", "features",
)
REQUIRED_FEATURE_FIELDS = ("name", "dtype", "definition", "logic_version", "status", "since")
FEATURE_STATUSES = ("active", "deprecated")
FEATURE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

# Kinds of contract change (see diff_contract). The breaking ones break any model that reads
# the affected column.
ADDED, DEPRECATED, CHANGED, REMOVED, KEYS, METADATA = (
    "added", "deprecated", "changed", "removed", "keys", "metadata")
BREAKING_KINDS = (CHANGED, REMOVED, KEYS)


# ---------------------------------------------------------------------------
# Loading & validation
# ---------------------------------------------------------------------------

def load_yaml(path: str | Path, variables: dict[str, str] | None = None) -> dict[str, Any]:
    """Load a YAML file, substituting ${var} placeholders in string values."""
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return _substitute(data, variables or {})


def _substitute(obj: Any, variables: dict[str, str]) -> Any:
    if isinstance(obj, dict):
        return {k: _substitute(v, variables) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_substitute(v, variables) for v in obj]
    if isinstance(obj, str) and "${" in obj:
        for name, value in variables.items():
            obj = obj.replace(f"${{{name}}}", value)
    return obj


def parse_version(version: str) -> tuple[int, int, int]:
    m = SEMVER_RE.match(str(version))
    if not m:
        raise ValueError(f"version must be MAJOR.MINOR.PATCH, got {version!r}")
    return tuple(int(x) for x in m.groups())  # type: ignore[return-value]


def validate_contract(contract: dict[str, Any]) -> list[str]:
    """Return a list of problems with the contract (empty list = valid)."""
    errors = [f"missing field: {f}" for f in REQUIRED_CONTRACT_FIELDS if f not in contract]
    if errors:
        return errors

    try:
        parse_version(contract["version"])
    except ValueError as e:
        errors.append(str(e))

    if not contract["primary_keys"]:
        errors.append("primary_keys must not be empty")

    seen: set[str] = set()
    key_cols = set(contract["primary_keys"]) | {contract["timestamp_key"]}
    for feat in contract["features"]:
        name = feat.get("name", "<unnamed>")
        missing = [f for f in REQUIRED_FEATURE_FIELDS if f not in feat]
        if missing:
            errors.append(f"feature {name}: missing {', '.join(missing)}")
            continue
        if not FEATURE_NAME_RE.match(name):
            errors.append(f"feature {name}: name must be lower_snake_case")
        if name in seen:
            errors.append(f"feature {name}: duplicated")
        if name in key_cols:
            errors.append(f"feature {name}: clashes with a key column")
        seen.add(name)
        if feat["status"] not in FEATURE_STATUSES:
            errors.append(f"feature {name}: status must be one of {FEATURE_STATUSES}")
        if feat["status"] == "deprecated":
            if not feat.get("sunset_date"):
                errors.append(f"feature {name}: deprecated features need a sunset_date")
            if not feat.get("replaced_by"):
                errors.append(f"feature {name}: deprecated features need replaced_by (or 'none')")
    return errors


def feature_names(contract: dict[str, Any], status: str | None = None) -> list[str]:
    return [f["name"] for f in contract["features"] if status is None or f["status"] == status]


# ---------------------------------------------------------------------------
# Change detection (the PR gate)
# ---------------------------------------------------------------------------

def diff_contract(base: dict[str, Any], proposed: dict[str, Any]) -> list[dict[str, str]]:
    """Every difference between the contract on main and the one in the PR.

    Returns [{"feature", "kind", "message"}] where kind is one of:
      added       new feature                                  (minor bump)
      deprecated  active -> deprecated                         (minor bump)
      changed     definition / logic_version / dtype edited    (major bump; breaks readers)
      removed     feature dropped from the contract            (major bump; breaks readers)
      keys        table, primary or timestamp key changed      (breaks every reader)
      metadata    description, owner, SLA, ... edited          (patch bump)
    """
    changes: list[dict[str, str]] = []

    def add(feature: str, kind: str, message: str) -> None:
        changes.append({"feature": feature, "kind": kind, "message": message})

    if base["table"] != proposed["table"]:
        add("<table>", KEYS, f"table renamed {base['table']} -> {proposed['table']}")
    if (list(base["primary_keys"]) != list(proposed["primary_keys"])
            or base["timestamp_key"] != proposed["timestamp_key"]):
        add("<keys>", KEYS, "primary/timestamp key changed")

    base_feats = {f["name"]: f for f in base["features"]}
    prop_feats = {f["name"]: f for f in proposed["features"]}
    for name, old in base_feats.items():
        new = prop_feats.get(name)
        if new is None:
            add(name, REMOVED, "removed")
            continue
        if old["dtype"] != new["dtype"]:
            add(name, CHANGED, f"dtype changed {old['dtype']} -> {new['dtype']}")
        if (old["logic_version"] != new["logic_version"]
                or _norm(old["definition"]) != _norm(new["definition"])):
            add(name, CHANGED, "logic/definition changed in place")
        if old["status"] == "active" and new["status"] == "deprecated":
            add(name, DEPRECATED, f"deprecated (sunset {new.get('sunset_date')}, "
                                  f"replaced by {new.get('replaced_by')})")
    for name in prop_feats:
        if name not in base_feats:
            add(name, ADDED, "new feature")
    for field in ("description", "owner", "support_channel", "refresh", "freshness_sla_hours", "sources"):
        if base.get(field) != proposed.get(field):
            add("<table>", METADATA, f"{field} changed")
    return changes


def change_problems(base: dict[str, Any], proposed: dict[str, Any],
                    changes: list[dict[str, str]]) -> list[str]:
    """Rule breaks that block the PR whoever the consumers are.

    The pipeline can only add columns, drop columns and recompute values, so key and dtype
    changes have to ship as a new table / a new column. The version must be bumped by
    semver and the changelog must say why.
    """
    problems = []
    for c in changes:
        if c["kind"] == KEYS:
            problems.append(f"{c['message']}: publish a new table (e.g. <table>_v2) instead")
        elif c["message"].startswith("dtype changed"):
            problems.append(f"{c['feature']}: {c['message']} — publish {next_version_name(c['feature'])} instead")

    try:
        old_v, new_v = parse_version(base["version"]), parse_version(proposed["version"])
    except ValueError as e:
        return problems + [str(e)]
    if not changes:
        return problems + ([] if new_v == old_v else ["version bumped but nothing changed"])
    kinds = {c["kind"] for c in changes}
    if kinds & set(BREAKING_KINDS):
        required, label = (old_v[0] + 1, 0, 0), "major"
    elif kinds & {ADDED, DEPRECATED}:
        required, label = (old_v[0], old_v[1] + 1, 0), "minor"
    else:
        required, label = (old_v[0], old_v[1], old_v[2] + 1), "patch"
    if new_v < required:
        problems.append(f"version {proposed['version']} too low: this change needs a {label} bump "
                        f"(>= {'.'.join(map(str, required))})")
    elif not any(str(e.get("version")) == str(proposed["version"]) for e in proposed.get("changelog", [])):
        problems.append(f"changelog has no entry for {proposed['version']}")
    return problems


def downstream_breaks(changes: list[dict[str, str]],
                      consumers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Which live consumers a change would break.

    consumers: [{"team", "model", "version", "alias", "features"}] — every aliased model
    version that reads the table, with the columns it was trained on (see live_consumers in
    governance/check_change.py). A consumer breaks when a column it reads is changed or
    removed, or when the table's keys change.
    """
    breaks = []
    for c in changes:
        if c["kind"] not in BREAKING_KINDS:
            continue
        for consumer in consumers:
            if c["kind"] == KEYS or c["feature"] in consumer["features"]:
                breaks.append({**c, "consumer": consumer})
    return breaks


def next_version_name(name: str) -> str:
    m = re.match(r"^(.*)_v(\d+)$", name)
    return f"{m.group(1)}_v{int(m.group(2)) + 1}" if m else f"{name}_v2"


def _norm(text: str) -> str:
    return " ".join(str(text).split())


# ---------------------------------------------------------------------------
# Unity Catalog DDL derived from the contract
# ---------------------------------------------------------------------------

_SPARK_TYPES = {"int": "INT", "bigint": "BIGINT", "double": "DOUBLE", "float": "FLOAT",
                "string": "STRING", "boolean": "BOOLEAN", "date": "DATE", "timestamp": "TIMESTAMP"}


def spark_type(dtype: str) -> str:
    try:
        return _SPARK_TYPES[dtype.lower()]
    except KeyError:
        raise ValueError(f"unsupported dtype {dtype!r}; use one of {sorted(_SPARK_TYPES)}") from None


def _sql_str(value: Any) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def schema_sync_statements(contract: dict[str, Any], existing_columns: list[str]) -> list[str]:
    """ALTER statements that bring an existing table's columns in line with the contract.

    Adds new features; drops columns that left the contract (the PR gate only lets that
    through when no live model reads them).
    """
    table = contract["table"]
    keys = set(contract["primary_keys"]) | {contract["timestamp_key"]}
    existing = {c.lower() for c in existing_columns}
    wanted = {f["name"]: f for f in contract["features"]}
    stmts = []
    to_add = [f for n, f in wanted.items() if n not in existing]
    if to_add:
        cols = ", ".join(f"{f['name']} {spark_type(f['dtype'])}" for f in to_add)
        stmts.append(f"ALTER TABLE {table} ADD COLUMNS ({cols})")
    for col in sorted(existing - set(wanted) - keys):
        stmts.append(f"ALTER TABLE {table} DROP COLUMN {col}")
    return stmts


def uc_metadata_statements(contract: dict[str, Any]) -> list[str]:
    """COMMENT / SET TAGS statements so the contract is discoverable in Catalog Explorer."""
    table = contract["table"]
    stmts = [
        f"COMMENT ON TABLE {table} IS {_sql_str(_norm(contract['description']))}",
        f"ALTER TABLE {table} SET TAGS ("
        f"'shared' = 'true', "
        f"'feature_owner' = {_sql_str(contract['owner'])}, "
        f"'support_channel' = {_sql_str(contract['support_channel'])}, "
        f"'contract_version' = {_sql_str(contract['version'])}, "
        f"'refresh' = {_sql_str(contract['refresh'])}, "
        f"'freshness_sla_hours' = {_sql_str(contract['freshness_sla_hours'])})",
    ]
    for feat in contract["features"]:
        col = f"{table} ALTER COLUMN {feat['name']}"
        stmts.append(f"ALTER TABLE {col} COMMENT {_sql_str(_norm(feat['definition']))}")
        tags = {"status": feat["status"], "since": feat["since"], "logic_version": feat["logic_version"]}
        if feat["status"] == "deprecated":
            tags["sunset_date"] = feat["sunset_date"]
            tags["replaced_by"] = feat["replaced_by"]
        tag_sql = ", ".join(f"{_sql_str(k)} = {_sql_str(v)}" for k, v in tags.items())
        stmts.append(f"ALTER TABLE {col} SET TAGS ({tag_sql})")
    return stmts


# ---------------------------------------------------------------------------
# Consumer side
# ---------------------------------------------------------------------------

def lookup_specs(feature_config: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn a consumer's feature_dependencies block into FeatureLookup kwargs.

    Kept as plain dicts so it is testable without the feature-engineering client:
        [FeatureLookup(**spec) for spec in lookup_specs(cfg)]
    """
    specs = []
    for dep in feature_config.get("feature_dependencies", []):
        if not dep.get("feature_names"):
            raise ValueError(f"{dep['table']}: pin feature_names explicitly — never consume 'all columns'")
        specs.append({
            "table_name": dep["table"],
            "feature_names": list(dep["feature_names"]),
            "lookup_key": dep["lookup_key"],
            "timestamp_lookup_key": dep.get("timestamp_lookup_key"),
        })
    return specs


def deprecated_dependencies(feature_config: dict[str, Any],
                            contracts: dict[str, dict[str, Any]]) -> list[str]:
    """Warn consumers about pinned features the producer has deprecated."""
    warnings = []
    for dep in feature_config.get("feature_dependencies", []):
        contract = contracts.get(dep["table"])
        if not contract:
            continue
        by_name = {f["name"]: f for f in contract["features"]}
        for name in dep["feature_names"]:
            feat = by_name.get(name)
            if feat is None:
                warnings.append(f"{dep['table']}.{name}: not in the producer contract")
            elif feat["status"] == "deprecated":
                warnings.append(f"{dep['table']}.{name}: deprecated, sunset {feat['sunset_date']}, "
                                f"migrate to {feat['replaced_by']}")
    return warnings
