# Feature-contract utilities shared by the jobs (producer.py, provision_access.py) and the
# governance checks. A plain Python module (not a notebook), so it imports the same way
# everywhere: notebooks put shared/ on sys.path; tests and CI import it directly.
#
# Plain functions only — no top-level spark/dbutils — so the same logic runs in the
# workspace, in pytest, and in the PR gate (governance/check_change.py).

import hashlib
import re
from pathlib import Path
from typing import Any

import yaml

REQUIRED_CONTRACT_FIELDS = (
    "name", "environments", "version", "owner", "support_channel", "description",
    "primary_keys", "timestamp_key", "refresh", "freshness_sla_hours", "features",
)
ENVIRONMENTS = ("dev", "staging", "prod")  # bundle targets; every contract says where it lives in each
REQUIRED_FEATURE_FIELDS = ("name", "dtype", "definition", "status", "since")
# Where each flat field comes from in the ODCS file, for error messages.
ODCS_FIELDS = {
    "name": "schema[0].name", "environments": "servers (one per environment)", "version": "version",
    "owner": "team (a member with role: owner)", "support_channel": "support", "description": "description.purpose",
    "primary_keys": "a primaryKey property", "timestamp_key": "a property with customProperty timestampKey: true",
    "refresh": "slaProperties frequency", "freshness_sla_hours": "slaProperties latency",
    "features": "schema[0].properties", "dtype": "physicalType", "definition": "description",
    "status": "customProperty status", "since": "customProperty since",
}
BASE_SQL = "_base"  # the query skeleton in a table's features/ folder; the rest are <feature>.sql
FEATURE_STATUSES = ("active", "deprecated")
FEATURE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
ACCESS_PRIVILEGES = ("SELECT", "MODIFY")  # table privileges a role's access can grant

# Kinds of change (see diff_contract). The breaking ones change what an existing reader gets.
ADDED, DEPRECATED, CHANGED, REMOVED, KEYS, METADATA, REVOKED = (
    "added", "deprecated", "changed", "removed", "keys", "metadata", "revoked")
BREAKING_KINDS = (CHANGED, REMOVED, KEYS)


# ---------------------------------------------------------------------------
# Loading & validation
#
# A contract is an ODCS v3.0.1 data contract (https://bitol-io.github.io/open-data-contract-standard/v3.0.1/):
# one databricks server per environment (catalog, schema, and its sources as a
# customProperty), one schema object for the table, one property per key or feature.
# from_odcs() turns it into the flat dict the rest of this module works on:
#
#   name: customer_features                     schema[0].name
#   environments:                               servers, by environment
#     dev:  {catalog: ..., schema: ..., sources: {transactions: <catalog.schema.table>}}
#   owner, support_channel, description         team (role owner), support, description.purpose
#   primary_keys, timestamp_key                 properties with primaryKey / timestampKey
#   refresh, freshness_sla_hours                slaProperties frequency / latency
#   features: [{name, dtype, definition, status, since, sunset_date, replaced_by}]
#   access_list: [{principal, privileges}]      roles (access read / write)
#   changelog, online                           customProperties
#
# load_table(path, env) picks one environment: the table is <catalog>.<schema>.<name>, and
# each ${<source name>} in the SQL becomes that environment's source table.
# ---------------------------------------------------------------------------

ODCS_SCHEMA = Path(__file__).with_name("odcs-json-schema-v3.0.1.json")
ACCESS_LEVELS = {"read": ["SELECT"], "write": ["SELECT", "MODIFY"]}  # ODCS role access -> UC privileges
_HOURS = {"h": 1, "hour": 1, "hours": 1, "d": 24, "day": 24, "days": 24}


def _custom(obj: dict[str, Any]) -> dict[str, Any]:
    """An ODCS object's customProperties as {property: value}."""
    items = obj.get("customProperties") if isinstance(obj, dict) else None
    return {c.get("property"): c.get("value") for c in items or [] if isinstance(c, dict)}


def from_odcs(odcs: dict[str, Any]) -> dict[str, Any]:
    """The flat contract (see above) for an ODCS data contract. Lenient: whatever is missing
    is left out, for validate_contract to report. A contract that isn't ODCS (no apiVersion)
    is returned as is (bare access_list names get SELECT) — only so the PR that migrates a
    table can compare it with main; remove once every contract on main is ODCS."""
    if "apiVersion" not in odcs:
        if isinstance(odcs.get("access_list"), list):
            odcs = {**odcs, "access_list": [{"principal": e, "privileges": ["SELECT"]} if isinstance(e, str) else e
                                            for e in odcs["access_list"]]}
        return odcs
    c: dict[str, Any] = {"id": odcs.get("id"), "version": odcs.get("version"), "problems": []}
    tables = odcs.get("schema") or []
    if len(tables) != 1 or not isinstance(tables[0], dict):
        c["problems"].append("schema must have exactly one entry: the feature table")
    table = tables[0] if tables and isinstance(tables[0], dict) else {}
    if table.get("name"):
        c["name"] = table["name"]

    envs: dict[str, Any] = {}
    for server in odcs.get("servers") or []:
        env = server.get("environment")
        if env in envs:
            c["problems"].append(f"servers: more than one {env} server")
        envs[env] = {"catalog": server.get("catalog"), "schema": server.get("schema"),
                     "sources": _custom(server).get("sources")}
    if envs:
        c["environments"] = envs

    owners = [m.get("username") for m in odcs.get("team") or [] if m.get("role") == "owner"]
    if len(owners) == 1:
        c["owner"] = owners[0]
    elif owners:
        c["problems"].append(f"team: one member with role owner, not {len(owners)}")
    if odcs.get("support"):
        c["support_channel"] = odcs["support"][0].get("channel")
    if isinstance(odcs.get("description"), dict) and odcs["description"].get("purpose"):
        c["description"] = odcs["description"]["purpose"]

    props = [p for p in table.get("properties") or [] if isinstance(p, dict)]
    keys = sorted((p for p in props if p.get("primaryKey")), key=lambda p: p.get("primaryKeyPosition", -1))
    if keys:
        c["primary_keys"] = [p["name"] for p in keys]
    ts = [p["name"] for p in props if _custom(p).get("timestampKey") is True]
    if len(ts) == 1:
        c["timestamp_key"] = ts[0]
    elif ts:
        c["problems"].append(f"schema: one timestampKey property, not {len(ts)} ({', '.join(ts)})")
    c["features"] = []
    for p in props:
        if p.get("primaryKey") or p.get("name") in ts:
            continue
        custom = _custom(p)
        feat = {"name": p.get("name"), "dtype": p.get("physicalType"), "definition": p.get("description"),
                "status": custom.get("status"), "since": custom.get("since"),
                "sunset_date": custom.get("sunsetDate"), "replaced_by": custom.get("replacedBy")}
        c["features"].append({k: v for k, v in feat.items() if v is not None})

    sla = {s.get("property"): s for s in odcs.get("slaProperties") or [] if isinstance(s, dict)}
    if "frequency" in sla:
        c["refresh"] = f"{sla['frequency'].get('value')}{sla['frequency'].get('unit') or ''}"
    if "latency" in sla:
        value, unit = sla["latency"].get("value"), str(sla["latency"].get("unit", "")).lower()
        if isinstance(value, (int, float)) and unit in _HOURS:
            c["freshness_sla_hours"] = value * _HOURS[unit]
        else:
            c["problems"].append("slaProperties latency: needs a number of hours (unit h) or days (unit d)")

    if "roles" in odcs:
        roles = odcs["roles"]
        c["access_list"] = roles if not isinstance(roles, list) else [
            {"principal": r.get("role"),
             "privileges": ACCESS_LEVELS.get(str(r.get("access", "read")).lower(), [r.get("access")])}
            if isinstance(r, dict) else r for r in roles]
    custom = _custom(odcs)
    c["changelog"] = custom.get("changelog") or []
    if "online" in custom:
        c["online"] = custom["online"]
    return c


def odcs_errors(path: str | Path) -> list[str]:
    """Where the contract file breaks the ODCS v3.0.1 JSON schema (empty list = compliant)."""
    import json

    import jsonschema  # only the PR gate needs it

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    validator = jsonschema.Draft201909Validator(json.loads(ODCS_SCHEMA.read_text(encoding="utf-8")),
                                                format_checker=jsonschema.FormatChecker())
    return [f"ODCS {'.'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}"
            for e in sorted(validator.iter_errors(data), key=lambda e: list(map(str, e.absolute_path)))]


PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
SQL_RESERVED = {"features"}  # ${features} in _base.sql is where the feature expressions go
FULL_NAME_RE = re.compile(r"^[^.\s]+\.[^.\s]+\.[^.\s]+$")


def load_yaml(path: str | Path, variables: dict[str, str] | None = None) -> dict[str, Any]:
    """Load a contract (from_odcs), substituting ${var} placeholders in string values."""
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return _substitute(from_odcs(data), variables or {})


def resolve(contract: dict[str, Any], env: str) -> dict[str, Any]:
    """The contract as it applies in `env`: adds "table" (<catalog>.<schema>.<name>), "sources"
    (that environment's source tables) and "environment". Resolving again is a no-op."""
    envs = contract.get("environments")
    if not isinstance(envs, dict) or not isinstance(envs.get(env), dict):
        raise ValueError(f"no {env} server (where the table lives in {env})")
    e = envs[env]
    sources = e.get("sources") if isinstance(e.get("sources"), dict) else {}
    return {**contract, "table": f"{e.get('catalog')}.{e.get('schema')}.{contract.get('name')}",
            "sources": list(sources.values()), "environment": env}


def load_table(path: str | Path, env: str,
               legacy_vars: dict[str, str] | None = None) -> tuple[dict[str, Any], dict[str, str]]:
    """(contract, {feature: SQL}) for one feature table in `env`.

    Raises ValueError when `env` isn't defined or a ${placeholder} can't be filled. A contract
    from before environments: existed (a `table:` with ${catalog}-style placeholders) is
    filled from legacy_vars instead — only so the PR that migrates a table can compare it
    with main; remove once every contract on main has environments:.
    """
    raw = load_yaml(path)
    if "environments" not in raw and "table" in raw:
        contract, values = _substitute(raw, legacy_vars or {}), dict(legacy_vars or {})
    else:
        contract = resolve(raw, env)
        values = dict(raw["environments"][env].get("sources") or {})
    sql = load_feature_sql(path, values)
    unfilled = sorted({m for text in [*sql.values()] for m in PLACEHOLDER_RE.findall(_strip_comments(text))}
                      - SQL_RESERVED) + sorted(set(PLACEHOLDER_RE.findall(yaml.safe_dump(contract))))
    if unfilled:
        names = ", ".join("${" + n + "}" for n in dict.fromkeys(unfilled))
        raise ValueError(f"{Path(path).parent.name}: {names} not defined — add it to every server's sources")
    return contract, sql


def _substitute(obj: Any, variables: dict[str, str]) -> Any:
    if isinstance(obj, dict):
        return {k: _substitute(v, variables) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_substitute(v, variables) for v in obj]
    if isinstance(obj, str) and "${" in obj:
        for name, value in variables.items():
            obj = obj.replace(f"${{{name}}}", value)
    return obj


def feature_sql_dir(contract_path: str | Path) -> Path:
    """features/<table>/<contract>.yaml -> that same folder (contract and SQL live together)."""
    return Path(contract_path).parent


def load_feature_sql(contract_path: str | Path, variables: dict[str, str] | None = None) -> dict[str, str]:
    """{feature name: its SQL expression, "_base": the query skeleton} for a contract.
    Empty if the folder doesn't exist."""
    folder = feature_sql_dir(contract_path)
    return {f.stem: _substitute(f.read_text(encoding="utf-8"), variables or {})
            for f in sorted(folder.glob("*.sql"))} if folder.is_dir() else {}


def parse_version(version: str) -> tuple[int, int, int]:
    m = SEMVER_RE.match(str(version))
    if not m:
        raise ValueError(f"version must be MAJOR.MINOR.PATCH, got {version!r}")
    return tuple(int(x) for x in m.groups())  # type: ignore[return-value]


def validate_contract(contract: dict[str, Any]) -> list[str]:
    """Return a list of problems with the contract (empty list = valid)."""
    errors = list(contract.get("problems", []))
    errors += [f"missing field: {ODCS_FIELDS[f]}" for f in REQUIRED_CONTRACT_FIELDS if f not in contract]
    if errors:
        return errors

    try:
        parse_version(contract["version"])
    except ValueError as e:
        errors.append(str(e))

    if not contract["primary_keys"]:
        errors.append("primary_keys must not be empty")
    errors += _validate_environments(contract)

    seen: set[str] = set()
    key_cols = set(contract["primary_keys"]) | {contract["timestamp_key"]}
    for feat in contract["features"]:
        name = feat.get("name", "<unnamed>")
        missing = [f for f in REQUIRED_FEATURE_FIELDS if f not in feat]
        if missing:
            errors.append(f"feature {name}: missing {', '.join(ODCS_FIELDS.get(f, f) for f in missing)}")
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
                errors.append(f"feature {name}: deprecated features need a sunsetDate")
            if not feat.get("replaced_by"):
                errors.append(f"feature {name}: deprecated features need replacedBy (or 'none')")
    return errors + _validate_access(contract)


def _validate_environments(contract: dict[str, Any]) -> list[str]:
    if not FEATURE_NAME_RE.match(str(contract["name"])):
        return [f"schema[0].name {contract['name']!r} must be lower_snake_case (it's the table name)"]
    envs = contract["environments"]
    if not isinstance(envs, dict):
        return ["servers must give each environment its catalog, schema and sources"]
    errors = [f"servers: {env} missing (every table needs a server per environment: {', '.join(ENVIRONMENTS)})"
              for env in ENVIRONMENTS if env not in envs]
    errors += [f"servers: environment {env} isn't a bundle target ({', '.join(ENVIRONMENTS)})"
               for env in envs if env not in ENVIRONMENTS]
    source_names = None
    for env in [e for e in ENVIRONMENTS if e in envs]:
        e = envs[env] if isinstance(envs[env], dict) else {}
        for field in ("catalog", "schema"):
            if not isinstance(e.get(field), str) or not e[field].strip():
                errors.append(f"servers {env}: needs a {field}")
        sources = e.get("sources")
        if not isinstance(sources, dict) or not sources:
            errors.append(f"servers {env}: needs a sources customProperty (name: catalog.schema.table)")
            continue
        for name, table in sources.items():
            if not FEATURE_NAME_RE.match(str(name)) or name in SQL_RESERVED:
                errors.append(f"servers {env} sources: {name!r} must be lower_snake_case (not 'features')")
            if not FULL_NAME_RE.match(str(table)):
                errors.append(f"servers {env} sources.{name}: {table!r} must be catalog.schema.table")
        if source_names is None:
            source_names = set(sources)
        elif set(sources) != source_names:
            errors.append(f"servers {env}: sources {sorted(sources)} must have the same names as "
                          f"the other servers {sorted(source_names)} (the SQL uses them)")
    return errors


def _validate_access(contract: dict[str, Any]) -> list[str]:
    if "access_list" not in contract:
        return []
    if not isinstance(contract["access_list"], list):
        return ["roles must be a list of {role, access}"]
    errors, seen = [], set()
    for entry in contract["access_list"]:
        if not isinstance(entry, dict):
            errors.append(f"roles: {entry!r} must be {{role, access}}")
            continue
        principal, privileges = entry.get("principal"), entry.get("privileges")
        if not isinstance(principal, str) or not principal.strip():
            errors.append("roles: every entry needs a role (the principal)")
            continue
        bad = [p for p in privileges or [] if str(p).upper() not in ACCESS_PRIVILEGES]
        if bad or not privileges:
            errors.append(f"roles {principal}: access {', '.join(map(str, bad))} must be one of {', '.join(ACCESS_LEVELS)}")
        if principal in seen:
            errors.append(f"roles {principal}: listed twice")
        seen.add(principal)
    return errors


def validate_sql(contract: dict[str, Any], sql: dict[str, str]) -> list[str]:
    """Every feature in the contract has exactly one SQL expression, and vice versa."""
    errors = []
    if BASE_SQL not in sql:
        return [f"missing {BASE_SQL}.sql (the query the feature expressions go into)"]
    if "${features}" not in _strip_comments(sql[BASE_SQL]):
        errors.append(f"{BASE_SQL}.sql must contain ${{features}} where the feature expressions go")
    names = {f["name"] for f in contract["features"]}
    errors += [f"feature {n}: no {n}.sql" for n in sorted(names - set(sql))]
    errors += [f"{n}.sql: not in the contract (add it to the schema properties or delete the file)"
               for n in sorted(set(sql) - names - {BASE_SQL})]
    errors += [f"{n}.sql: empty" for n in sorted(names & set(sql)) if not sql_norm(sql[n])]
    return errors


def feature_names(contract: dict[str, Any], status: str | None = None) -> list[str]:
    return [f["name"] for f in contract["features"] if status is None or f["status"] == status]


# ---------------------------------------------------------------------------
# Change detection (the PR gate)
#
# Features are immutable: once released, a feature's SQL and type never change. New logic
# ships as a new feature (<name>_v2); the old one is deprecated, then removed once nothing
# depends on it. That keeps every model's training data reproducible and means a release
# can't change what a reader gets — whether or not we can see the reader.
# ---------------------------------------------------------------------------

def diff_contract(base: dict[str, Any], proposed: dict[str, Any],
                  base_sql: dict[str, str] | None = None,
                  proposed_sql: dict[str, str] | None = None) -> list[dict[str, str]]:
    """Every difference between main and the PR, for one feature table (contract + SQL).

    Returns [{"feature", "kind", "message"}] where kind is one of:
      added       new feature                                    minor bump
      deprecated  active -> deprecated                           minor bump
      removed     feature dropped                                major bump, deprecated features only
      changed     a released feature's SQL or dtype edited       never allowed
      keys        table moved (any environment's catalog/schema,  never allowed
                  or its name), keys, or the query skeleton edited
      metadata    definition text, description, owner, SLA, ...  patch bump
                  (and access granted in roles)
      revoked     access removed from roles                     minor bump, with a warning
    SQL comments and whitespace don't count as changes. If main has no SQL for the table
    yet (the PR that first adds it), there's nothing to compare the SQL with.
    """
    base_sql, proposed_sql = base_sql or {}, proposed_sql or {}
    compare_sql = bool(base_sql)
    changes: list[dict[str, str]] = []

    def add(feature: str, kind: str, message: str) -> None:
        changes.append({"feature": feature, "kind": kind, "message": message})

    if isinstance(base.get("environments"), dict) and isinstance(proposed.get("environments"), dict):
        for env in ENVIRONMENTS:
            old, new = base["environments"].get(env), proposed["environments"].get(env)
            if not isinstance(new, dict):
                continue
            if not isinstance(old, dict):
                add("<table>", METADATA, f"{env} environment added")
                continue
            old_t = f"{old.get('catalog')}.{old.get('schema')}.{base.get('name')}"
            new_t = f"{new.get('catalog')}.{new.get('schema')}.{proposed.get('name')}"
            if old_t != new_t:
                add("<table>", KEYS, f"{env}: table moves {old_t} -> {new_t}")
            if old.get("sources") != new.get("sources"):
                add("<table>", METADATA, f"{env} sources changed")
    else:  # main's contract predates environments: compare the environment being checked
        if base["table"] != proposed["table"]:
            add("<table>", KEYS, f"table renamed {base['table']} -> {proposed['table']}")
        if list(base.get("sources", [])) != list(proposed.get("sources", [])):
            add("<table>", METADATA, "sources changed")
    if (list(base["primary_keys"]) != list(proposed["primary_keys"])
            or base["timestamp_key"] != proposed["timestamp_key"]):
        add("<keys>", KEYS, "primary/timestamp key changed")
    if compare_sql and sql_norm(base_sql.get(BASE_SQL, "")) != sql_norm(proposed_sql.get(BASE_SQL, "")):
        add("<query>", KEYS, f"{BASE_SQL}.sql changed (grain, window or source: changes every feature)")

    base_feats = {f["name"]: f for f in base["features"]}
    prop_feats = {f["name"]: f for f in proposed["features"]}
    for name, old in base_feats.items():
        new = prop_feats.get(name)
        if new is None:
            add(name, REMOVED, f"removed (was {old['status']})")
            continue
        if old["dtype"] != new["dtype"]:
            add(name, CHANGED, f"dtype changed {old['dtype']} -> {new['dtype']}")
        if compare_sql and sql_norm(base_sql.get(name, "")) != sql_norm(proposed_sql.get(name, "")):
            add(name, CHANGED, f"SQL changed in place ({name}.sql)")
        if _norm(old["definition"]) != _norm(new["definition"]):
            add(name, METADATA, "definition text changed")
        if old["status"] == "active" and new["status"] == "deprecated":
            add(name, DEPRECATED, f"deprecated (sunset {new.get('sunset_date')}, "
                                  f"replaced by {new.get('replaced_by')})")
        elif old["status"] == "deprecated" and new["status"] == "active":
            add(name, METADATA, "un-deprecated")
    old_access, new_access = access_grants(base) or set(), access_grants(proposed) or set()
    for principal, privilege in sorted(new_access - old_access):
        add("<access>", METADATA, f"{principal} granted {privilege}")
    for principal, privilege in sorted(old_access - new_access):
        add("<access>", REVOKED, f"{principal} loses {privilege}")
    for name in prop_feats:
        if name not in base_feats:
            add(name, ADDED, "new feature")
    for field in ("description", "owner", "support_channel", "refresh", "freshness_sla_hours"):
        if base.get(field) != proposed.get(field):
            add("<table>", METADATA, f"{field} changed")
    return changes


def change_problems(base: dict[str, Any], proposed: dict[str, Any],
                    changes: list[dict[str, str]]) -> list[str]:
    """Rule breaks that block the PR, whoever depends on the table."""
    problems = []
    base_feats = {f["name"]: f for f in base["features"]}
    for c in changes:
        if c["kind"] == KEYS:
            problems.append(f"{c['message']}: publish a new table (e.g. <table>_v2) instead")
        elif c["kind"] == CHANGED:
            new = next_version_name(c["feature"])
            problems.append(f"{c['feature']}: {c['message']} — released features are immutable. Add the new "
                            f"logic as {new}, deprecate {c['feature']}, remove it once nothing reads it")
        elif c["kind"] == REMOVED and base_feats[c["feature"]]["status"] != "deprecated":
            problems.append(f"{c['feature']}: active features can't be removed — deprecate it "
                            "(sunsetDate, replacedBy) in one release and remove it in a later one")

    try:
        old_v, new_v = parse_version(base["version"]), parse_version(proposed["version"])
    except ValueError as e:
        return problems + [str(e)]
    if not changes:
        return problems + ([] if new_v == old_v else ["version bumped but nothing changed"])
    kinds = {c["kind"] for c in changes}
    if kinds & set(BREAKING_KINDS):
        required, label = (old_v[0] + 1, 0, 0), "major"
    elif kinds & {ADDED, DEPRECATED, REVOKED}:
        required, label = (old_v[0], old_v[1] + 1, 0), "minor"
    else:
        required, label = (old_v[0], old_v[1], old_v[2] + 1), "patch"
    if new_v < required:
        problems.append(f"version {proposed['version']} too low: this change needs a {label} bump "
                        f"(>= {'.'.join(map(str, required))})")
    elif not any(str(e.get("version")) == str(proposed["version"]) for e in proposed.get("changelog", [])):
        problems.append(f"changelog has no entry for {proposed['version']}")
    return problems


def change_warnings(base: dict[str, Any], changes: list[dict[str, str]], today: str) -> list[str]:
    """Allowed, but worth flagging: removing a feature before the sunset date it promised, and
    taking access away (lineage records users and jobs, not groups, so the gate can't tell
    whether that team still reads the table)."""
    base_feats = {f["name"]: f for f in base["features"]}
    return [f"{c['feature']}: removed before its promised sunset date {base_feats[c['feature']]['sunset_date']}"
            for c in changes
            if c["kind"] == REMOVED and str(base_feats[c["feature"]].get("sunset_date", "")) > today] + [
        f"access: {c['message']} on {base['table']} — make sure they've moved off it"
        for c in changes if c["kind"] == REVOKED]


def downstream_breaks(changes: list[dict[str, str]],
                      dependencies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Which dependencies a change would break.

    dependencies: [{"kind", "name", "detail", "features", ...}] — see
    governance/dependencies.py. "features" is the list of the table's columns it reads, or
    None when that couldn't be determined (then it is assumed to read every column). A
    dependency breaks when a column it reads is changed or removed, or when the keys change.
    """
    breaks = []
    for c in changes:
        if c["kind"] not in BREAKING_KINDS:
            continue
        for dep in dependencies:
            if c["kind"] == KEYS or dep["features"] is None or c["feature"] in dep["features"]:
                breaks.append({**c, "dependency": dep})
    return breaks


def next_version_name(name: str) -> str:
    m = re.match(r"^(.*)_v(\d+)$", name)
    return f"{m.group(1)}_v{int(m.group(2)) + 1}" if m else f"{name}_v2"


def _norm(text: str) -> str:
    return " ".join(str(text).split())


def _strip_comments(sql: str) -> str:
    return re.sub(r"--[^\n]*", "", str(sql))


def sql_norm(sql: str) -> str:
    """SQL without -- comments or extra whitespace: what counts as a change."""
    return _norm(_strip_comments(sql))


# ---------------------------------------------------------------------------
# The feature query: _base.sql with every feature's expression inserted
# ---------------------------------------------------------------------------

def features_sql(contract: dict[str, Any], sql: dict[str, str]) -> str:
    select = ",\n  ".join(f"CAST({sql_norm(sql[f['name']])} AS {spark_type(f['dtype'])}) AS {f['name']}"
                          for f in contract["features"])
    return _strip_comments(sql[BASE_SQL]).replace("${features}", select)


def expression_hash(sql: str) -> str:
    """Short fingerprint of a feature's SQL (comments/whitespace ignored), tagged on its column."""
    return hashlib.sha256(sql_norm(sql).encode()).hexdigest()[:12]


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
    through for deprecated features nothing depends on).
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


def uc_metadata_statements(contract: dict[str, Any], sql: dict[str, str]) -> list[str]:
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
        tags = {"status": feat["status"], "since": feat["since"], "sql_hash": expression_hash(sql[feat["name"]])}
        if feat["status"] == "deprecated":
            tags["sunset_date"] = feat["sunset_date"]
            tags["replaced_by"] = feat["replaced_by"]
        tag_sql = ", ".join(f"{_sql_str(k)} = {_sql_str(v)}" for k, v in tags.items())
        stmts.append(f"ALTER TABLE {col} SET TAGS ({tag_sql})")
    return stmts


# ---------------------------------------------------------------------------
# Access: Unity Catalog grants derived from the contract's roles
# ---------------------------------------------------------------------------

def access_entries(contract: dict[str, Any]) -> list[dict[str, Any]] | None:
    """The contract's roles as [{"principal", "privileges"}], or None when the contract has
    no roles (then its grants aren't managed)."""
    if "access_list" not in contract:
        return None
    return [{"principal": e["principal"], "privileges": [str(p).upper() for p in e["privileges"]]}
            for e in contract["access_list"]]


def access_grants(contract: dict[str, Any]) -> set[tuple[str, str]] | None:
    """{(principal, privilege)} the contract asks for, or None when access isn't managed."""
    entries = access_entries(contract)
    return None if entries is None else {(e["principal"], p) for e in entries for p in e["privileges"]}


def grant_statements(contract: dict[str, Any], current: set[tuple[str, str]], keep: set[str]) -> list[str]:
    """GRANT / REVOKE statements that make the table's grants match its roles exactly.

    current: the (principal, privilege) pairs granted on the table itself today, limited to
    ACCESS_PRIVILEGES. keep: principals never revoked (the table owner, the job's identity).
    Listed principals also get USE CATALOG / USE SCHEMA so they can reach the table; those
    are only ever granted, since other tables share the catalog and schema.
    """
    wanted = access_grants(contract)
    if wanted is None:
        return []
    table = contract["table"]
    catalog, schema = table.split(".")[0], ".".join(table.split(".")[:2])
    stmts = []
    for principal in sorted({p for p, _ in wanted}):
        missing = sorted(priv for p, priv in wanted - current if p == principal)
        if missing:
            stmts.append(f"GRANT {', '.join(missing)} ON TABLE {table} TO `{principal}`")
        stmts.append(f"GRANT USE CATALOG ON CATALOG {catalog} TO `{principal}`")
        stmts.append(f"GRANT USE SCHEMA ON SCHEMA {schema} TO `{principal}`")
    for principal, privilege in sorted(current - wanted):
        if principal not in keep:
            stmts.append(f"REVOKE {privilege} ON TABLE {table} FROM `{principal}`")
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
