# Databricks notebook source
# Governance — who depends on a feature table (or one column of it)?
#
# Run by the producer before any breaking change or removal. Two sources:
#   1. Declared dependencies (authoritative for "active"): every registered model version
#      that currently holds an alias (@Champion, @Challenger, ...) and whose
#      feature_dependencies tag includes the table/column. Changing or removing a column
#      one of these reads would break it (the PR gate, check_change.py, asks the same thing).
#   2. Unity Catalog lineage (system.access.table_lineage / column_lineage): who read the
#      table recently — jobs, notebooks, models, dashboards. Shown for context; catches
#      consumers that never declared anything. Lineage can lag by several minutes.
#
# Output: the teams to talk to before a breaking change (`affected_consumers`).
# fail_if_active=true turns this into the deprecation gate (fails while active consumers remain).

# COMMAND ----------

import json

import mlflow
from mlflow.tracking import MlflowClient

for w in ("table", "column", "fail_if_active", "catalog"):
    dbutils.widgets.text(w, "")
table = dbutils.widgets.get("table").strip()
column = dbutils.widgets.get("column").strip() or None
fail_if_active = dbutils.widgets.get("fail_if_active").strip().lower() == "true"
catalog = dbutils.widgets.get("catalog").strip() or table.split(".")[0]
target = f"{table}.{column}" if column else table
print(f"Consumers of {target}")

# COMMAND ----------

# 1. Declared, active dependencies from the model registry.
mlflow.set_registry_uri("databricks-uc")
client = MlflowClient()
active = []
from databricks.sdk import WorkspaceClient

w = WorkspaceClient()
model_names = [m.full_name
               for s in w.schemas.list(catalog_name=catalog) if s.name != "information_schema"
               for m in w.registered_models.list(catalog_name=catalog, schema_name=s.name)]
for name in model_names:
    rm = client.get_registered_model(name)
    owner = (rm.tags or {}).get("consumer_team", "unknown")
    for alias in rm.aliases or []:
        alias_name, version = (alias.alias, alias.version) if hasattr(alias, "alias") else (alias, rm.aliases[alias])
        mv = client.get_model_version(rm.name, version)
        deps = json.loads((mv.tags or {}).get("feature_dependencies", "{}"))
        cols = deps.get(table)
        if cols is None or (column and column not in cols):
            continue
        active.append({"team": owner, "model": rm.name, "version": int(version),
                       "alias": alias_name, "features": cols})

print("\nActive model dependencies (aliased versions):")
for a in active:
    print(f"  {a['team']:<8} {a['model']} v{a['version']} @{a['alias']}  uses {a['features']}")
if not active:
    print("  none")

# COMMAND ----------

# 2. Lineage — recent readers (last 90 days).
try:
    if column:
        lineage = spark.sql(f"""
          SELECT entity_type, entity_id, target_table_full_name, target_column_name, created_by,
                 max(event_time) AS last_read
          FROM system.access.column_lineage
          WHERE source_table_full_name = '{table}' AND source_column_name = '{column}'
            AND event_time >= current_timestamp() - INTERVAL 90 DAYS
          GROUP BY ALL ORDER BY last_read DESC""")
    else:
        lineage = spark.sql(f"""
          SELECT entity_type, entity_id, target_type, target_table_full_name, created_by,
                 max(event_time) AS last_read
          FROM system.access.table_lineage
          WHERE source_table_full_name = '{table}'
            AND event_time >= current_timestamp() - INTERVAL 90 DAYS
          GROUP BY ALL ORDER BY last_read DESC""")
    print(f"\nLineage: {lineage.count()} recent reader(s)")
    display(lineage)
except Exception as e:  # system tables not enabled / no access
    print(f"\nLineage unavailable ({type(e).__name__}); relying on declared dependencies only.")

# COMMAND ----------

teams = sorted({a["team"] for a in active})
print("\nTeams to talk to before changing it:")
print(f"affected_consumers: [{', '.join(teams)}]")
dbutils.jobs.taskValues.set("affected_consumers", teams)

summary = {"target": target, "active_consumers": active, "affected_consumers": teams}
if fail_if_active and active:
    raise AssertionError(
        f"{target} still has {len(active)} active consumer(s): "
        + ", ".join(f"{a['model']} v{a['version']} @{a['alias']}" for a in active)
        + " — they must migrate before removal.")

dbutils.notebook.exit(json.dumps(summary))
