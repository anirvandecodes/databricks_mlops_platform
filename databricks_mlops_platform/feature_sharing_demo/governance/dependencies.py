"""Who depends on a feature table, column by column — collected from every source Unity
Catalog has, so a producer can decide whether a change is safe.

Models (things that keep reading the feature after training):
  1. UC lineage (/api/2.0/lineage-tracking/table-lineage): every model version logged with
     fe.log_model from a training set on this table, in any catalog.
  2. The feature_dependencies tag on aliased model versions in the table's catalog (written
     by team_b_consumer/notebooks/02_train_with_lookups). A fallback for lineage lag.
  For every model found, all of its *live* versions are checked — those holding an alias
  (@champion, ...) or served by a serving endpoint — and the exact columns each one looks up
  are read from its feature_spec.yaml (packaged by fe.log_model; it is what score_batch and
  serving use). A live version whose columns can't be read counts as reading every column.

Jobs, pipelines and dashboards (things that read the table directly):
  3. system.access.column_lineage over the last `lookback_days`: the columns each entity read
     in its *latest* run, so a job that has moved off a column stops counting. The producer
     (anything that writes the table) is excluded. Interactive notebooks and ad hoc queries
     are reported as warnings, not dependencies.

Every source is required: if one can't be read, the error is returned and the caller must
treat the result as unknown (check_change.py blocks).
"""

import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import yaml

_START = time.monotonic()


def log(message: str) -> None:
    """Progress line on stderr: shows what is being checked while the report stays on stdout."""
    print(f"[{time.monotonic() - _START:6.1f}s] {message}", file=sys.stderr, flush=True)


# Lineage entity types that are someone poking at the data, not something that runs again.
AD_HOC_ENTITY_TYPES = {"NOTEBOOK", "DBSQL_QUERY", "QUERY", None}


@dataclass
class Dependency:
    kind: str                       # model | job | pipeline | dashboard | <lineage entity type>
    name: str
    detail: str                     # e.g. "v2 @champion (team_b)" or "last read ... by ..."
    features: list[str] | None      # columns of the table it reads; None = unknown (assume all)
    source: str                     # where the columns came from
    blocking: bool = True           # False = reported as a warning only


@dataclass
class Report:
    table: str
    dependencies: list[Dependency] = field(default_factory=list)
    not_counted: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def find_dependencies(table: str, lookback_days: int = 30, w: Any = None) -> Report | None:
    """All dependencies of `table`, or None if the table isn't published in this workspace."""
    from databricks.sdk import WorkspaceClient

    w = w or WorkspaceClient()
    log(f"Finding dependencies of {table}")
    if not w.tables.exists(table).table_exists:
        log("  table isn't published in this workspace → nothing can depend on it")
        return None
    log("  table exists in Unity Catalog")
    report = Report(table)
    for name, step in (("models", _models), ("column lineage", _lineage_readers)):
        try:
            step(w, table, report, lookback_days)
        except Exception as e:  # any source missing = can't decide
            report.errors.append(f"could not read {name}: {type(e).__name__}: {e}")
            log(f"  ✗ could not read {name}: {type(e).__name__}: {e}")
    log(f"  done: {sum(d.blocking for d in report.dependencies)} dependency(ies), "
        f"{sum(not d.blocking for d in report.dependencies)} ad hoc reader(s), {len(report.errors)} error(s)")
    return report


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

def _models(w: Any, table: str, report: Report, lookback_days: int) -> None:
    log("  [models 1/4] UC lineage: model versions logged from this table (fe.log_model)")
    lineage = w.api_client.do("GET", "/api/2.0/lineage-tracking/table-lineage",
                              query={"table_name": table, "include_entity_lineage": "true"})
    found = sorted({(d["modelInfo"]["model_name"], d["modelInfo"]["version"])
                    for d in lineage.get("downstreams", []) if "modelInfo" in d})
    log("               " + (", ".join(f"{n} v{v}" for n, v in found) or "none"))
    names = {n for n, _ in found}
    log(f"  [models 2/4] fallback: models tagged feature_dependencies in catalog {table.split('.')[0]}")
    tagged = _tagged_models(w, table)
    log("               " + (", ".join(sorted(tagged)) or "none")
        + (f"  (not in lineage yet: {', '.join(sorted(tagged - names))})" if tagged - names else ""))
    names |= tagged
    log("  [models 3/4] serving endpoints that serve UC model versions")
    served = _served_versions(w)
    log("               " + (", ".join(f"{n} v{v} on {e}" for n, vs in served.items() for v, e in vs) or "none"))

    log("  [models 4/4] live versions and the exact columns each reads (feature_spec.yaml)")
    for name in sorted(names):
        rm = _uc("registered-models/get", w, name=name)["registered_model"]
        team = _tags(rm).get("consumer_team", "unknown")
        live: dict[int, list[str]] = {}
        for a in rm.get("aliases", []):
            live.setdefault(int(a["version"]), []).append(f"@{a['alias']}")
        for v, endpoint in served.get(name, []):
            live.setdefault(v, []).append(f"served by {endpoint}")
        log(f"               {name} (team {team})")
        for v in _all_versions(w, name):
            if v not in live:
                report.not_counted.append(f"model     {name} v{v} (no alias, not served)")
                log(f"                 v{v}: not live (no alias, not served) → not counted")
        for v, why in sorted(live.items()):
            features, source = _version_features(w, name, v, table)
            if features == []:
                log(f"                 v{v} {' '.join(why)}: doesn't read this table")
                continue  # this version doesn't use the table
            log(f"                 v{v} {' '.join(why)}: reads "
                + ("EVERY column (unknown)" if features is None else ", ".join(features)) + f"  [{source}]")
            report.dependencies.append(Dependency(
                "model", name, f"v{v} {' '.join(why)} ({team})", features, source))


def _tagged_models(w: Any, table: str) -> set[str]:
    catalog = table.split(".")[0]
    names = set()
    for schema in w.schemas.list(catalog_name=catalog):
        if schema.name == "information_schema":
            continue
        for m in w.registered_models.list(catalog_name=catalog, schema_name=schema.name):
            rm = _uc("registered-models/get", w, name=m.full_name)["registered_model"]
            for a in rm.get("aliases", []):
                mv = _uc("model-versions/get", w, name=m.full_name, version=a["version"])["model_version"]
                if table in json.loads(_tags(mv).get("feature_dependencies", "{}")):
                    names.add(m.full_name)
    return names


def _served_versions(w: Any) -> dict[str, list[tuple[int, str]]]:
    served: dict[str, list[tuple[int, str]]] = {}
    for ep in w.serving_endpoints.list():
        config = ep.config
        for e in (config.served_entities or []) if config else []:
            if e.entity_name and e.entity_version and e.entity_name.count(".") == 2:
                served.setdefault(e.entity_name, []).append((int(e.entity_version), ep.name))
    return served


def _all_versions(w: Any, name: str) -> list[int]:
    return sorted(int(v.version) for v in w.model_versions.list(name))


def _version_features(w: Any, name: str, version: int, table: str) -> tuple[list[str] | None, str]:
    """Columns of `table` this model version looks up. [] = doesn't use it, None = unknown."""
    path = f"/Models/{name.replace('.', '/')}/{version}/data/feature_store/feature_spec.yaml"
    try:
        spec = yaml.safe_load(w.files.download(path).contents.read())
        return feature_spec_columns(spec, table), "feature spec"
    except Exception:
        pass
    mv = _uc("model-versions/get", w, name=name, version=str(version))["model_version"]
    deps = json.loads(_tags(mv).get("feature_dependencies", "{}"))
    if deps:
        return deps.get(table, []), "feature_dependencies tag"
    return None, "unknown: no feature spec or tag, so assumed to read every column"


def feature_spec_columns(spec: dict[str, Any], table: str) -> list[str]:
    """The feature columns a feature_spec.yaml (fe.log_model) looks up from `table`."""
    cols = []
    for entry in spec.get("input_columns", []):
        for name, info in entry.items():
            if (info or {}).get("table_name") == table and info.get("source") == "feature_store":
                cols.append(info.get("feature_name", name))
    return cols


# ---------------------------------------------------------------------------
# Jobs, pipelines, dashboards (column lineage)
# ---------------------------------------------------------------------------

_READERS_SQL = """
WITH reads AS (
  SELECT entity_type, entity_id, entity_run_id, source_column_name, event_time, created_by
  FROM system.access.column_lineage
  WHERE source_table_full_name = :table AND entity_id IS NOT NULL
    AND event_date >= date_sub(current_date(), :days)),
writers AS (
  SELECT DISTINCT entity_id FROM system.access.table_lineage
  WHERE target_table_full_name = :table AND entity_id IS NOT NULL
    AND event_date >= date_sub(current_date(), :days)),
latest AS (
  SELECT entity_type, entity_id, max_by(entity_run_id, event_time) AS run,
         max(event_time) AS last_read, max_by(created_by, event_time) AS created_by
  FROM reads GROUP BY ALL)
SELECT l.entity_type, l.entity_id, string(l.last_read), l.created_by,
       l.entity_id IN (SELECT entity_id FROM writers) AS producer,
       to_json(sort_array(collect_set(r.source_column_name))) AS columns
FROM latest l JOIN reads r ON r.entity_id = l.entity_id AND r.entity_run_id <=> l.run
GROUP BY ALL
ORDER BY 1, 2
"""


def _lineage_readers(w: Any, table: str, report: Report, lookback_days: int) -> None:
    from databricks.sdk.service.sql import StatementParameterListItem, StatementState

    warehouse = _warehouse_id(w)
    log(f"  [lineage]    system.access.column_lineage, last {lookback_days} days, latest run per reader "
        f"(warehouse {warehouse})")
    stmt = w.statement_execution.execute_statement(
        warehouse_id=warehouse, statement=_READERS_SQL, wait_timeout="50s",
        parameters=[StatementParameterListItem(name="table", value=table),
                    StatementParameterListItem(name="days", value=str(lookback_days), type="INT")])
    while stmt.status.state in (StatementState.PENDING, StatementState.RUNNING):
        time.sleep(2)
        stmt = w.statement_execution.get_statement(stmt.statement_id)
    if stmt.status.state != StatementState.SUCCEEDED:
        raise RuntimeError(stmt.status.error.message if stmt.status.error else stmt.status.state)

    rows = stmt.result.data_array or []
    log(f"               {len(rows)} reader(s)" + ("" if rows else " — nothing read this table recently"))
    for etype, eid, last_read, by, producer, cols in rows:
        kind = etype.lower()
        name = _entity_name(w, etype, eid)
        if producer == "true":
            report.not_counted.append(f"producer  {kind} {name} (writes this table)")
            log(f"               {kind} {name}: producer (writes this table) → not counted")
            continue
        log(f"               {kind} {name}: read {', '.join(json.loads(cols))} at {last_read[:16]}"
            + ("" if etype not in AD_HOC_ENTITY_TYPES else " → ad hoc, warning only"))
        report.dependencies.append(Dependency(
            kind, name, f"latest read {last_read[:16]} by {by}", json.loads(cols),
            f"column lineage, last {lookback_days} days", blocking=etype not in AD_HOC_ENTITY_TYPES))


def _warehouse_id(w: Any) -> str:
    if os.environ.get("DATABRICKS_WAREHOUSE_ID"):
        return os.environ["DATABRICKS_WAREHOUSE_ID"]
    warehouses = list(w.warehouses.list())
    if not warehouses:
        raise RuntimeError("no SQL warehouse to query system.access lineage (set DATABRICKS_WAREHOUSE_ID)")
    # Serverless starts in seconds; prefer it, then anything already running.
    warehouses.sort(key=lambda wh: (not wh.enable_serverless_compute, str(wh.state) != "State.RUNNING"))
    return warehouses[0].id


def _entity_name(w: Any, etype: str, eid: str) -> str:
    try:
        if etype == "JOB":
            return w.jobs.get(int(eid)).settings.name
        if etype == "PIPELINE":
            return w.pipelines.get(eid).name
    except Exception:
        pass
    return eid


# ---------------------------------------------------------------------------

def _uc(endpoint: str, w: Any, **query: str) -> dict[str, Any]:
    return w.api_client.do("GET", f"/api/2.0/mlflow/unity-catalog/{endpoint}", query=query)


def _tags(obj: dict[str, Any]) -> dict[str, str]:
    return {t["key"]: t["value"] for t in obj.get("tags", [])}
