# Databricks notebook source
# Team B — discover shared features and check the ones we depend on.
#
# Consumers find features through Unity Catalog, not through Slack or copied notebooks:
# every shared table carries tags (shared, owner, support_channel, contract_version,
# refresh, freshness SLA) and every column a definition comment + status tag. This step
# also warns when a feature Team B pins has been deprecated by its producer, and fails if
# a pinned feature no longer exists.

# COMMAND ----------

# MAGIC %pip install -q pyyaml
# MAGIC %restart_python

# COMMAND ----------

# MAGIC %run ../../shared/feature_contract_utils

# COMMAND ----------

for w in ("feature_config_path", "catalog", "producer_schema", "consumer_schema"):
    dbutils.widgets.text(w, "")
variables = {k: dbutils.widgets.get(k).strip() for k in ("catalog", "producer_schema", "consumer_schema")}
catalog = variables["catalog"]
cfg = load_yaml(dbutils.widgets.get("feature_config_path").strip(), variables)

# COMMAND ----------

# 1. What shared feature tables exist, who owns them, how fresh are they?
shared = spark.sql(f"""
  SELECT concat_ws('.', catalog_name, schema_name, table_name) AS table,
         map_from_entries(collect_list(struct(tag_name, tag_value))) AS tags
  FROM {catalog}.information_schema.table_tags
  GROUP BY catalog_name, schema_name, table_name
  HAVING tags['shared'] = 'true'
""").collect()

print("Shared feature tables in", catalog)
for row in shared:
    t = row["tags"]
    print(f"  {row['table']}\n    owner={t.get('feature_owner')}  support={t.get('support_channel')}  "
          f"contract={t.get('contract_version')}  refresh={t.get('refresh')}  "
          f"sla={t.get('freshness_sla_hours')}h")

# COMMAND ----------

# 2. Column catalogue: definition + lifecycle status for each feature.
display(spark.sql(f"""
  SELECT c.table_name, c.ordinal_position AS pos, c.column_name, c.data_type, c.comment AS definition,
         max(CASE WHEN ct.tag_name = 'status' THEN ct.tag_value END)      AS status,
         max(CASE WHEN ct.tag_name = 'since' THEN ct.tag_value END)       AS since,
         max(CASE WHEN ct.tag_name = 'sunset_date' THEN ct.tag_value END) AS sunset_date,
         max(CASE WHEN ct.tag_name = 'replaced_by' THEN ct.tag_value END) AS replaced_by
  FROM {catalog}.information_schema.columns c
  LEFT JOIN {catalog}.information_schema.column_tags ct
    ON ct.schema_name = c.table_schema AND ct.table_name = c.table_name AND ct.column_name = c.column_name
  WHERE c.table_schema = '{variables['producer_schema']}'
  GROUP BY ALL
  ORDER BY c.table_name, pos
"""))

# COMMAND ----------

# 3. Check Team B's pinned dependencies against what the producer publishes.
problems, warnings = [], []
for dep in cfg["feature_dependencies"]:
    table = dep["table"]
    _, schema, name = table.split(".")
    if not spark.catalog.tableExists(table):
        problems.append(f"{table}: table not found")
        continue
    tags = {r["tag_name"]: r["tag_value"] for r in spark.sql(f"""
        SELECT tag_name, tag_value FROM {catalog}.information_schema.table_tags
        WHERE schema_name = '{schema}' AND table_name = '{name}'""").collect()}
    status = {r["column_name"]: r for r in spark.sql(f"""
        SELECT column_name,
               max(CASE WHEN tag_name = 'status' THEN tag_value END) AS status,
               max(CASE WHEN tag_name = 'sunset_date' THEN tag_value END) AS sunset_date,
               max(CASE WHEN tag_name = 'replaced_by' THEN tag_value END) AS replaced_by
        FROM {catalog}.information_schema.column_tags
        WHERE schema_name = '{schema}' AND table_name = '{name}'
        GROUP BY column_name""").collect()}
    columns = {c.name for c in spark.table(table).schema}

    print(f"{table}: producer contract {tags.get('contract_version')}, "
          f"Team B built against {dep.get('contract_version')}")
    for feat in dep["feature_names"]:
        if feat not in columns:
            problems.append(f"{table}.{feat}: pinned but no longer published — raise a change request "
                            f"with {tags.get('feature_owner')} ({tags.get('support_channel')})")
        elif status.get(feat) and status[feat]["status"] == "deprecated":
            s = status[feat]
            warnings.append(f"{table}.{feat}: DEPRECATED by {tags.get('feature_owner')}, sunset {s['sunset_date']}, "
                            f"migrate to {s['replaced_by']}")
        else:
            print(f"  ok  {feat}")

for w in warnings:
    print("  WARNING", w)
dbutils.jobs.taskValues.set("deprecation_warnings", warnings)
if problems:
    raise ValueError("Dependency check failed:\n  " + "\n  ".join(problems))

# COMMAND ----------

import json

dbutils.notebook.exit(json.dumps({"shared_tables": [r["table"] for r in shared], "deprecation_warnings": warnings}))
