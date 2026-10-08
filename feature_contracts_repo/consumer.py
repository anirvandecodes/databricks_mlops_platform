# Databricks notebook source
# Consumer — read the shared feature table and show its Unity Catalog lineage.
#
# Reading the table from this notebook is itself what registers lineage: UC records that
# this job read customer_features, and that customer_features was built from transactions.
# The read below creates the edge; the queries then print both directions.

# COMMAND ----------

dbutils.widgets.text("table", "")
table = dbutils.widgets.get("table").strip()
if table.count(".") != 2:
    raise ValueError(f"table must be catalog.schema.table, got {table!r}")
_, schema, name = table.split(".")

# COMMAND ----------

# Read the feature table — this is the consumer's dependency on it, and what UC records.
df = spark.table(table)
print(f"Read {table}: {df.count():,} rows, {len(df.columns)} columns")
display(df.orderBy(df.as_of_date.desc(), df.customer_id).limit(10))

# COMMAND ----------

# Upstream: what customer_features was built from. Downstream: who reads it.
# system.access.table_lineage can take a few minutes to reflect the newest runs.
print(f"Lineage for {table}\n")

print("Upstream (sources this table was built from):")
display(spark.sql(f"""
  SELECT DISTINCT source_table_full_name AS upstream, entity_type, event_time
  FROM system.access.table_lineage
  WHERE target_table_schema = '{schema}' AND target_table_name = '{name}'
    AND source_table_full_name IS NOT NULL
  ORDER BY event_time DESC
"""))

print("Downstream (who reads this table):")
display(spark.sql(f"""
  SELECT DISTINCT target_table_full_name AS downstream, entity_type, entity_run_id, event_time
  FROM system.access.table_lineage
  WHERE source_table_schema = '{schema}' AND source_table_name = '{name}'
  ORDER BY event_time DESC
"""))
