# Databricks notebook source
# Producer — build every feature table under features/ from its own self-contained folder.
#
#   for each features/<table>/: contract (YAML) + SQL files, in this environment
#     ->  UC feature table (PK + TIMESERIES, CDF) + discovery comments/tags
#
# Each folder in features/ is one feature table: a <name>.yaml contract (schema, meaning,
# lifecycle, and where it lives and what it reads in each environment) plus _base.sql (the
# query skeleton) and one <feature>.sql per feature (how it's computed, inserted into
# _base.sql). Add a feature table by dropping in a new folder and re-running — nothing else
# to change. The job passes only the environment (the bundle target); each contract decides
# its own catalog, schema and sources for it.
#
# Task 2 of the feature_tables job (after the demo's mock_sources); task 3
# (provision_access.py) then applies each contract's access_list.

# COMMAND ----------

# MAGIC %pip install -q pyyaml
# MAGIC %restart_python

# COMMAND ----------

import os
import re
import sys
from pathlib import Path

# Workspace files: this notebook's folder is the working directory; shared/ is beside it.
sys.path.insert(0, os.path.abspath("shared"))
import feature_contract_utils as fcu  # noqa: E402

dbutils.widgets.text("features_root", "")
dbutils.widgets.text("environment", "")
features_root = Path(dbutils.widgets.get("features_root").strip())
environment = dbutils.widgets.get("environment").strip()
if environment not in fcu.ENVIRONMENTS:
    raise ValueError(f"environment must be one of {fcu.ENVIRONMENTS}, got {environment!r}")

_SPARK_TYPES = {"int": "INT", "bigint": "BIGINT", "double": "DOUBLE", "float": "FLOAT",
                "string": "STRING", "boolean": "BOOLEAN", "date": "DATE", "timestamp": "TIMESTAMP"}

def strip_comments(s: str) -> str:
    return re.sub(r"--[^\n]*", "", s)

def q(value) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"

# COMMAND ----------

def publish(folder: Path) -> str:
    """Build one feature table from its self-contained folder (contract + SQL)."""
    contract, sql = fcu.load_table(sorted(folder.glob("*.yaml"))[0], environment)
    problems = fcu.validate_contract(contract) + fcu.validate_sql(contract, sql)
    if problems:
        raise ValueError(f"features/{folder.name}: " + "; ".join(problems))

    table = contract["table"]
    catalog, schema = table.split(".")[:2]
    if not spark.catalog.databaseExists(f"{catalog}.{schema}"):
        if not any(r[0] == catalog for r in spark.sql("SHOW CATALOGS").collect()):
            raise ValueError(f"{table}: catalog {catalog} doesn't exist in this workspace (the producer "
                             f"never creates catalogs; ask the platform team, or fix environments.{environment})")
        spark.sql(f"CREATE SCHEMA IF NOT EXISTS {catalog}.{schema}")

    key, ts_key = contract["primary_keys"][0], contract["timestamp_key"]
    print(f"\n=== {table} ({environment}) @ contract {contract['version']} ===")
    print("Features:", ", ".join(f"{f['name']}[{f['status']}]" for f in contract["features"]))

    # Feature logic: _base.sql with each feature's expression inserted (point-in-time
    # correct: every expression only sees transactions strictly before as_of_date).
    select = ",\n  ".join(
        f"CAST({strip_comments(sql[f['name']]).strip()} AS {_SPARK_TYPES[f['dtype']]}) AS {f['name']}"
        for f in contract["features"])
    spark.sql(strip_comments(sql["_base"]).replace("${features}", select)).createOrReplaceTempView("new_features")

    # Create the table on first publish (PK + TIMESERIES key makes it a UC feature table), then merge.
    if not spark.catalog.tableExists(table):
        cols = ",\n  ".join(f"{f['name']} {_SPARK_TYPES[f['dtype']]}" for f in contract["features"])
        spark.sql(f"""
        CREATE TABLE {table} (
          {key} BIGINT NOT NULL,
          {ts_key} DATE NOT NULL,
          {cols},
          CONSTRAINT {table.split('.')[-1]}_pk PRIMARY KEY ({key}, {ts_key} TIMESERIES)
        )
        TBLPROPERTIES (delta.enableChangeDataFeed = true)
        """)
        print(f"Created {table}")
    else:
        # Bring an existing table's columns in line with the contract: add new features, drop
        # columns that left it (the PR gate only lets that through once nothing reads them).
        if spark.sql(f"SHOW TBLPROPERTIES {table} ('delta.columnMapping.mode')").first()[1] != "name":
            spark.sql(f"ALTER TABLE {table} SET TBLPROPERTIES ('delta.columnMapping.mode' = 'name')")
        for stmt in fcu.schema_sync_statements(contract, spark.table(table).columns):
            print(stmt)
            spark.sql(stmt)

    cols = [key, ts_key] + [f["name"] for f in contract["features"]]
    spark.sql(f"""
    MERGE INTO {table} AS tgt
    USING new_features AS src
      ON tgt.{key} = src.{key} AND tgt.{ts_key} = src.{ts_key}
    WHEN MATCHED THEN UPDATE SET {", ".join(f"{c} = src.{c}" for c in cols[2:])}
    WHEN NOT MATCHED THEN INSERT ({", ".join(cols)}) VALUES ({", ".join(f"src.{c}" for c in cols)})
    """)

    # Comments + tags from the contract, so the table is discoverable in Catalog Explorer.
    spark.sql(f"COMMENT ON TABLE {table} IS {q(' '.join(str(contract['description']).split()))}")
    spark.sql(f"ALTER TABLE {table} SET TAGS ("
              f"'shared' = 'true', "
              f"'feature_owner' = {q(contract['owner'])}, "
              f"'support_channel' = {q(contract['support_channel'])}, "
              f"'contract_version' = {q(contract['version'])}, "
              f"'refresh' = {q(contract['refresh'])}, "
              f"'freshness_sla_hours' = {q(contract['freshness_sla_hours'])})")
    for feat in contract["features"]:
        col = f"{table} ALTER COLUMN {feat['name']}"
        spark.sql(f"ALTER TABLE {col} COMMENT {q(' '.join(str(feat['definition']).split()))}")
        spark.sql(f"ALTER TABLE {col} SET TAGS ('status' = {q(feat['status'])}, 'since' = {q(feat['since'])})")
    return table

folders = sorted(d for d in features_root.iterdir() if d.is_dir() and any(d.glob("*.yaml")))
if not folders:
    raise ValueError(f"no feature-table folders (with a *.yaml contract) under {features_root}")
published = [publish(folder) for folder in folders]

# COMMAND ----------

print("Published:", ", ".join(published))
for table in published:
    key_cols = ", ".join(c.name for c in spark.table(table).schema[:2])
    display(spark.sql(f"SELECT * FROM {table} ORDER BY {key_cols} DESC LIMIT 5"))
