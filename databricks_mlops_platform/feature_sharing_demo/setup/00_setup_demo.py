# Databricks notebook source
# Demo fixture: (re)creates the three demo schemas and mock data.
#
#   <catalog>.<raw_schema>.transactions        source data Team A's pipeline reads
#   <catalog>.<consumer_schema>.default_labels Team B's training spine (customer, as_of_date, label)
#   <catalog>.<producer_schema>                empty — Team A's pipeline creates the feature table
#
# The data is shaped so excluding refunds matters: risky customers issue more refunds, so
# counting refunds as activity (txn_count_30d) blurs the risk signal — which is why someone
# is tempted to "fix" txn_count_30d in place, the change the PR gate blocks. DROPS the demo schemas — demo catalog only.

# COMMAND ----------

import numpy as np
import pandas as pd

dbutils.widgets.text("catalog", "")
dbutils.widgets.text("raw_schema", "feature_demo_raw")
dbutils.widgets.text("producer_schema", "team_a_features")
dbutils.widgets.text("consumer_schema", "team_b_ml")
dbutils.widgets.text("producer_group", "")
dbutils.widgets.text("consumer_group", "")

catalog = dbutils.widgets.get("catalog").strip()
raw_schema = dbutils.widgets.get("raw_schema").strip()
producer_schema = dbutils.widgets.get("producer_schema").strip()
consumer_schema = dbutils.widgets.get("consumer_schema").strip()
producer_group = dbutils.widgets.get("producer_group").strip()
consumer_group = dbutils.widgets.get("consumer_group").strip()
if not catalog:
    raise ValueError("catalog is required")

# COMMAND ----------

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound

# Registered models are deleted explicitly before DROP SCHEMA ... CASCADE: neither CASCADE
# nor registered_models.delete removes a model that still has versions, so a re-run (every
# CI run of the walkthrough) would fail. Order matters: aliases, then versions, then model.
w = WorkspaceClient()
for schema in (raw_schema, producer_schema, consumer_schema):
    try:
        for m in w.registered_models.list(catalog_name=catalog, schema_name=schema):
            full = w.registered_models.get(m.full_name, include_aliases=True)
            for a in full.aliases or []:
                w.registered_models.delete_alias(m.full_name, a.alias_name)
            for v in w.model_versions.list(m.full_name):
                w.model_versions.delete(m.full_name, v.version)
            w.registered_models.delete(m.full_name)
    except NotFound:
        pass
    spark.sql(f"DROP SCHEMA IF EXISTS {catalog}.{schema} CASCADE")
    spark.sql(f"CREATE SCHEMA {catalog}.{schema}")
spark.sql(f"COMMENT ON SCHEMA {catalog}.{producer_schema} IS "
          "'Team A governed feature tables. Team A writes; other teams read. Contracts: feature_sharing_demo/team_a_producer/contracts'")
spark.sql(f"ALTER SCHEMA {catalog}.{producer_schema} SET TAGS ('feature_owner' = 'team_a', 'shared' = 'true')")
spark.sql(f"ALTER SCHEMA {catalog}.{consumer_schema} SET TAGS ('feature_owner' = 'team_b')")

# COMMAND ----------

# Mock transactions: 2,000 customers, Jan–Sep 2026.
rng = np.random.default_rng(7)
n_customers = 2_000
days = pd.date_range("2026-01-01", "2026-09-30", freq="D")
risk = rng.uniform(0, 1, n_customers)                       # latent risk, never published

purchase_rate = (8 * (1 - risk) + 2) / 30                   # low risk -> buys more
refund_rate = (9 * risk ** 2) / 30                          # high risk -> refunds a lot
purchases = rng.poisson(purchase_rate[:, None], (n_customers, len(days)))
refunds = rng.poisson(refund_rate[:, None], (n_customers, len(days)))

def expand(counts, is_refund):
    cust, day = np.nonzero(counts)
    reps = counts[cust, day]
    cust, day = np.repeat(cust, reps), np.repeat(day, reps)
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
spark.createDataFrame(txns).write.mode("overwrite").saveAsTable(f"{catalog}.{raw_schema}.transactions")
print(f"transactions: {len(txns):,} rows ({int(txns.is_refund.sum()):,} refunds)")

# COMMAND ----------

# Team B's spine: each customer observed at 4 random weekly snapshots; label = default in
# the following 30 days, driven by the latent risk.
snapshots = pd.date_range("2026-04-05", "2026-09-27", freq="W-SUN")
rows = []
for cid in range(n_customers):
    for d in rng.choice(snapshots, size=4, replace=False):
        p = 1 / (1 + np.exp(-(-3.0 + 4.5 * risk[cid])))
        rows.append((cid + 1, pd.Timestamp(d).date(), int(rng.uniform() < p)))
labels = pd.DataFrame(rows, columns=["customer_id", "as_of_date", "defaulted_next_30d"])
spark.createDataFrame(labels).write.mode("overwrite").saveAsTable(f"{catalog}.{consumer_schema}.default_labels")
print(f"default_labels: {len(labels):,} rows, default rate {labels.defaulted_next_30d.mean():.1%}")

# COMMAND ----------

# Access model: Team A writes its feature schema, Team B (and anyone else) reads it.
grants = []
if producer_group:
    grants += [f"GRANT USE SCHEMA, SELECT, MODIFY, CREATE TABLE ON SCHEMA {catalog}.{producer_schema} TO `{producer_group}`"]
if consumer_group:
    grants += [f"GRANT USE SCHEMA, SELECT ON SCHEMA {catalog}.{producer_schema} TO `{consumer_group}`",
               f"GRANT USE SCHEMA, SELECT, MODIFY, CREATE TABLE, CREATE MODEL ON SCHEMA {catalog}.{consumer_schema} TO `{consumer_group}`"]
for g in grants:
    spark.sql(g)
    print("applied:", g)
if not grants:
    print("No groups configured — in production the platform team applies (via Terraform):")
    print(f"  GRANT USE SCHEMA, SELECT, MODIFY, CREATE TABLE ON SCHEMA {catalog}.{producer_schema} TO `team_a_sp`")
    print(f"  GRANT USE SCHEMA, SELECT ON SCHEMA {catalog}.{producer_schema} TO `team_b`")
