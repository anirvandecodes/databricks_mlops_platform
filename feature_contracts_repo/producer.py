# Databricks notebook source
# Producer — build every feature table under features/ from its own self-contained folder.
#
#   mock transactions  ->  for each features/<table>/: contract (YAML) + SQL files
#                          ->  UC feature table (PK + TIMESERIES, CDF) + discovery comments/tags
#
# Each folder in features/ is one feature table: a <name>.yaml contract (schema, meaning,
# lifecycle) plus _base.sql (the query skeleton) and one <feature>.sql per feature (how it's
# computed, inserted into _base.sql). Add a feature table by dropping in a new folder and
# re-running — nothing else to change. Self-contained: this notebook generates its own mock
# source data, so a run needs only the folders and a catalog/schema to write into.
#
# Task 1 of the feature_tables job; task 2 (provision_access.py) then applies each
# contract's access_list.

# COMMAND ----------

# MAGIC %pip install -q pyyaml
# MAGIC %restart_python

# COMMAND ----------

import os
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

# Workspace files: this notebook's folder is the working directory; shared/ is beside it.
sys.path.insert(0, os.path.abspath("shared"))
import feature_contract_utils as fcu  # noqa: E402

dbutils.widgets.text("features_root", "")
dbutils.widgets.text("catalog", "")
dbutils.widgets.text("raw_schema", "feature_demo_raw")
dbutils.widgets.text("producer_schema", "team_a_features")

catalog = dbutils.widgets.get("catalog").strip()
raw_schema = dbutils.widgets.get("raw_schema").strip()
producer_schema = dbutils.widgets.get("producer_schema").strip()
features_root = Path(dbutils.widgets.get("features_root").strip())
if not catalog:
    raise ValueError("catalog is required")

variables = {"catalog": catalog, "raw_schema": raw_schema, "producer_schema": producer_schema}
source = f"{catalog}.{raw_schema}.transactions"

_SPARK_TYPES = {"int": "INT", "bigint": "BIGINT", "double": "DOUBLE", "float": "FLOAT",
                "string": "STRING", "boolean": "BOOLEAN", "date": "DATE", "timestamp": "TIMESTAMP"}

def substitute(text: str) -> str:
    for name, value in variables.items():
        text = text.replace(f"${{{name}}}", value)
    return text

def strip_comments(s: str) -> str:
    return re.sub(r"--[^\n]*", "", s)

def q(value) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"

# COMMAND ----------

# Mock transactions, shared by every feature table: 2,000 customers, Jan–Sep 2026,
# risky customers refund more.
spark.sql(f"CREATE SCHEMA IF NOT EXISTS {catalog}.{raw_schema}")
spark.sql(f"CREATE SCHEMA IF NOT EXISTS {catalog}.{producer_schema}")

rng = np.random.default_rng(7)
n_customers = 2_000
days = pd.date_range("2026-01-01", "2026-09-30", freq="D")
risk = rng.uniform(0, 1, n_customers)
purchases = rng.poisson(((8 * (1 - risk) + 2) / 30)[:, None], (n_customers, len(days)))
refunds = rng.poisson(((9 * risk ** 2) / 30)[:, None], (n_customers, len(days)))

def expand(counts, is_refund):
    cust, day = np.nonzero(counts)
    cust, day = np.repeat(cust, counts[cust, day]), np.repeat(day, counts[cust, day])
    amount = rng.lognormal(mean=3.5 - 0.5 * risk[cust], sigma=0.6)
    seconds = rng.integers(0, 86_400, len(cust))
    return pd.DataFrame({
        "customer_id": cust.astype("int64") + 1,
        "txn_ts": days.values[day] + pd.to_timedelta(seconds, unit="s"),
        "amount": np.round(np.where(is_refund, -amount, amount), 2),
        "is_refund": is_refund,
    })

txns = pd.concat([expand(purchases, False), expand(refunds, True)], ignore_index=True)
txns.insert(0, "txn_id", np.arange(1, len(txns) + 1))
spark.createDataFrame(txns).write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(source)
print(f"{source}: {len(txns):,} rows ({int(txns.is_refund.sum()):,} refunds)")

# COMMAND ----------

def publish(folder: Path) -> str:
    """Build one feature table from its self-contained folder (contract + SQL)."""
    contract = yaml.safe_load(substitute(next(folder.glob("*.yaml")).read_text(encoding="utf-8")))
    sql = {f.stem: substitute(f.read_text(encoding="utf-8")) for f in sorted(folder.glob("*.sql"))}

    table = contract["table"]
    key, ts_key = contract["primary_keys"][0], contract["timestamp_key"]
    print(f"\n=== {table} @ contract {contract['version']} ===")
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
