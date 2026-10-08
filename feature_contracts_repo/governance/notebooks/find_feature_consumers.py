# Databricks notebook source
# Governance — what depends on a feature table (or one column of it)?
#
# The same lookup the PR check runs (governance/dependencies.py), for a producer planning a
# change:
#   - live model versions (UC lineage + their feature specs; alias or serving endpoint)
#   - jobs, pipelines and dashboards (column lineage, latest run, last `lookback_days`)
#   - ad hoc notebook/query reads, as warnings
#
# column=<name> narrows it to one column. fail_if_active=true fails the run while anything
# still depends on it — the gate before removing a column.

# COMMAND ----------

import os
import sys

# Workspace files: this notebook's folder is the working directory; dependencies.py is one up.
sys.path.insert(0, os.path.abspath(".."))
from dependencies import find_dependencies  # noqa: E402

for w in ("table", "column", "fail_if_active", "lookback_days"):
    dbutils.widgets.text(w, "")
table = dbutils.widgets.get("table").strip()
column = dbutils.widgets.get("column").strip() or None
fail_if_active = dbutils.widgets.get("fail_if_active").strip().lower() == "true"
lookback_days = int(dbutils.widgets.get("lookback_days").strip() or 30)
target = f"{table}.{column}" if column else table

# COMMAND ----------

report = find_dependencies(table, lookback_days)
if report is None:
    dbutils.notebook.exit(f"{table} isn't published in this workspace.")
deps = [d for d in report.dependencies if column is None or d.features is None or column in d.features]

print(f"What depends on {target}:")
for title, group in (("Blocking (must migrate first)", [d for d in deps if d.blocking]),
                     ("Ad hoc reads (tell these people)", [d for d in deps if not d.blocking])):
    print(f"\n{title}:")
    for d in group:
        cols = "every column (unknown)" if d.features is None else ", ".join(d.features)
        print(f"  {d.kind:<9} {d.name} {d.detail}\n            reads {cols}  [{d.source}]")
    if not group:
        print("  none")
if report.not_counted:
    print("\nNot counted:")
    for n in report.not_counted:
        print(f"  {n}")
for e in report.errors:
    print(f"\nERROR {e}")

display(spark.createDataFrame(
    [(d.kind, d.name, d.detail, d.blocking, None if d.features is None else ", ".join(d.features), d.source)
     for d in deps] or [("none", "", "", False, "", "")],
    "kind string, name string, detail string, blocking boolean, reads string, source string"))

# COMMAND ----------

import json

blocking = [d for d in deps if d.blocking]
if report.errors:
    raise RuntimeError("Dependencies couldn't be fully checked:\n  " + "\n  ".join(report.errors))
if fail_if_active and blocking:
    raise AssertionError(f"{target} still has {len(blocking)} dependency(ies): "
                         + "; ".join(f"{d.kind} {d.name} {d.detail}" for d in blocking)
                         + " — they must migrate before removal.")
dbutils.notebook.exit(json.dumps({"target": target, "blocking": [f"{d.kind} {d.name}" for d in blocking],
                                  "ad_hoc": [f"{d.kind} {d.name}" for d in deps if not d.blocking]}))
