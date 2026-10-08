# Databricks notebook source
# Provision access — make each feature table's Unity Catalog grants match its contract.
#
#   features/<table>/<table>.yaml  access_list  ->  GRANT / REVOKE on the table
#
# Runs after producer.py in the feature_tables job, so every table it touches exists. The
# access_list is the source of truth: listed principals get their privileges (plus USE
# CATALOG / USE SCHEMA to reach the table), and SELECT/MODIFY is revoked from anyone not
# listed — except the table owner and this job's identity. A contract without an
# access_list is left alone.

# COMMAND ----------

# MAGIC %pip install -q pyyaml
# MAGIC %restart_python

# COMMAND ----------

import os
import sys
from pathlib import Path

# Workspace files: this notebook's folder is the working directory; shared/ is beside it.
sys.path.insert(0, os.path.abspath("shared"))
import feature_contract_utils as fcu  # noqa: E402

for w in ("features_root", "catalog", "raw_schema", "producer_schema"):
    dbutils.widgets.text(w, "")
features_root = Path(dbutils.widgets.get("features_root").strip())
variables = {w: dbutils.widgets.get(w).strip() for w in ("catalog", "raw_schema", "producer_schema")}
if not variables["catalog"]:
    raise ValueError("catalog is required")

me = spark.sql("SELECT current_user()").first()[0]

# COMMAND ----------

def provision(contract_path: Path) -> None:
    contract = fcu.load_yaml(contract_path, variables)
    table = contract["table"]
    if fcu.access_entries(contract) is None:
        print(f"{table}: no access_list — grants not managed")
        return
    problems = fcu.validate_contract(contract)
    if problems:
        raise ValueError(f"{contract_path}: " + "; ".join(problems))

    current = {(r["Principal"], r["ActionType"]) for r in spark.sql(f"SHOW GRANTS ON TABLE {table}").collect()
               if r["ObjectType"] == "TABLE" and r["ActionType"] in fcu.ACCESS_PRIVILEGES}
    owner = next((r["data_type"] for r in spark.sql(f"DESCRIBE TABLE EXTENDED {table}").collect()
                  if r["col_name"] == "Owner"), None)
    stmts = fcu.grant_statements(contract, current, keep={me, owner} - {None})

    print(f"\n=== {table} ===")
    for stmt in stmts:
        try:
            spark.sql(stmt)
        except Exception as e:  # most often: the group doesn't exist in the account
            raise RuntimeError(f"{stmt} failed — does the principal exist in the account? {e}") from e
        if not stmt.startswith("GRANT USE"):
            print(" ", stmt)
    readers = ", ".join(e["principal"] for e in fcu.access_entries(contract)) or "owner only"
    spark.sql(f"ALTER TABLE {table} SET TAGS ('readers' = '{readers.replace(chr(39), '')}')")
    print(f"  access: {readers}")


folders = sorted(d for d in features_root.iterdir() if d.is_dir() and any(d.glob("*.yaml")))
for folder in folders:
    provision(sorted(folder.glob("*.yaml"))[0])
