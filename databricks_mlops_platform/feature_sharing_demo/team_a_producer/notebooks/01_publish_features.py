# Databricks notebook source
# Team A — publish customer_features exactly as the released contract says.
#
#   contract (YAML, reviewed in Git)  ->  computed columns  ->  quality checks
#     ->  UC feature table (PK + TIMESERIES key, CDF)  ->  comments + tags for discovery
#
# The contract decides each feature's schema, meaning and status; its SQL file in
# team_a_producer/features/<table>/ decides how it's computed (run inside _base.sql). This
# notebook refuses to publish if either is invalid or they don't match one to one, and only
# ever changes the table schema the way the contract says (add new features; drop
# features removed after deprecation).

# COMMAND ----------

# MAGIC %pip install -q pyyaml
# MAGIC %restart_python

# COMMAND ----------

# MAGIC %run ../../shared/feature_contract_utils

# COMMAND ----------

dbutils.widgets.text("contract_path", "")
dbutils.widgets.text("catalog", "")
dbutils.widgets.text("raw_schema", "feature_demo_raw")
dbutils.widgets.text("producer_schema", "team_a_features")

variables = {k: dbutils.widgets.get(k).strip() for k in ("catalog", "raw_schema", "producer_schema")}
contract_path = dbutils.widgets.get("contract_path").strip()
contract = load_yaml(contract_path, variables)
sql = load_feature_sql(contract_path, variables)

problems = validate_contract(contract) or validate_sql(contract, sql)
if problems:
    raise ValueError("Contract invalid:\n  " + "\n  ".join(problems))

table = contract["table"]
source = contract["sources"][0]
key, ts_key = contract["primary_keys"][0], contract["timestamp_key"]
print(f"Publishing {table} @ contract {contract['version']}")
print("Features:", ", ".join(f"{f['name']}[{f['status']}]" for f in contract["features"]))

# COMMAND ----------

# Feature logic: _base.sql with each feature's expression inserted (point-in-time correct:
# every expression only sees transactions strictly before as_of_date).
features_df = spark.sql(features_sql(contract, sql))
features_df.createOrReplaceTempView("new_features")

# COMMAND ----------

# Quality gate — runs before anything is written. Failing here leaves the table untouched.
from pyspark.sql import functions as F

checks = features_df.agg(
    F.count("*").alias("rows"),
    F.countDistinct(key, ts_key).alias("distinct_keys"),
    F.sum(F.when(F.col(key).isNull() | F.col(ts_key).isNull(), 1).otherwise(0)).alias("null_keys"),
    *[F.sum(F.when(F.col(f["name"]).isNull(), 1).otherwise(0)).alias(f"null__{f['name']}")
      for f in contract["features"]],
    F.max(ts_key).alias("latest_snapshot"),
).first().asDict()
source_latest = spark.table(source).agg(F.max(F.to_date("txn_ts"))).first()[0]

failures = []
if checks["null_keys"]:
    failures.append(f"{checks['null_keys']} rows with null keys")
if checks["rows"] != checks["distinct_keys"]:
    failures.append("duplicate (key, as_of_date) rows")
failures += [f"{k[6:]} has {v} nulls" for k, v in checks.items() if k.startswith("null__") and v]
# Freshness: the newest snapshot must cover the newest source data within the SLA.
lag_hours = (source_latest - checks["latest_snapshot"]).days * 24
if lag_hours > contract["freshness_sla_hours"]:
    failures.append(f"stale: latest snapshot {checks['latest_snapshot']} lags source by {lag_hours}h")
if failures:
    raise AssertionError("Quality gate failed:\n  " + "\n  ".join(failures))
print(f"Quality gate passed: {checks['rows']:,} rows, latest snapshot {checks['latest_snapshot']}")

# COMMAND ----------

# Create the feature table on first publish; afterwards only evolve it as the contract says.
# A PRIMARY KEY with a TIMESERIES column is what makes a UC table a feature table that
# consumers can point-in-time join with FeatureLookup(timestamp_lookup_key=...).
if not spark.catalog.tableExists(table):
    cols = ",\n  ".join(f"{f['name']} {spark_type(f['dtype'])}" for f in contract["features"])
    spark.sql(f"""
    CREATE TABLE {table} (
      {key} BIGINT NOT NULL,
      {ts_key} DATE NOT NULL,
      {cols},
      CONSTRAINT customer_features_pk PRIMARY KEY ({key}, {ts_key} TIMESERIES)
    )
    TBLPROPERTIES (delta.enableChangeDataFeed = true, delta.columnMapping.mode = 'name')
    """)
    print(f"Created {table}")
else:
    existing = [c.name for c in spark.table(table).schema]
    for stmt in schema_sync_statements(contract, existing):
        print("schema:", stmt)
        spark.sql(stmt)

spark.sql(f"""
MERGE INTO {table} AS tgt
USING new_features AS src
  ON tgt.{key} = src.{key} AND tgt.{ts_key} = src.{ts_key}
WHEN MATCHED THEN UPDATE SET *
WHEN NOT MATCHED THEN INSERT *
""")

for stmt in uc_metadata_statements(contract, sql):
    spark.sql(stmt)

# COMMAND ----------

print(f"Published {table} @ {contract['version']}")
display(spark.sql(f"SELECT * FROM {table} ORDER BY {ts_key} DESC, {key} LIMIT 10"))
display(spark.sql(f"""
  SELECT column_name, tag_name, tag_value
  FROM {variables['catalog']}.information_schema.column_tags
  WHERE schema_name = '{variables['producer_schema']}' AND table_name = '{table.split('.')[-1]}'
  ORDER BY column_name, tag_name"""))

# COMMAND ----------

import json

dbutils.notebook.exit(json.dumps({
    "table": table, "contract_version": contract["version"], "rows": checks["rows"],
    "latest_snapshot": str(checks["latest_snapshot"]),
    "columns": [c.name for c in spark.table(table).schema],
    "deprecated": feature_names(contract, "deprecated"),
}))
