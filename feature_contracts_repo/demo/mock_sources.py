# Databricks notebook source
# DEMO ONLY — delete in a real repo, where contracts read real source tables.
#
# Writes the mock transactions table customer_features reads in this environment
# (environments.<env>.sources.transactions): 2,000 customers, Jan–Sep 2026, risky customers
# refund more. Task 1 of the feature_tables job.

# COMMAND ----------

import numpy as np
import pandas as pd

dbutils.widgets.text("table", "")
source = dbutils.widgets.get("table").strip()
if source.count(".") != 2:
    raise ValueError(f"table must be catalog.schema.table, got {source!r}")

# COMMAND ----------

# Mock transactions, shared by every feature table: 2,000 customers, Jan–Sep 2026,
# risky customers refund more.
spark.sql(f"CREATE SCHEMA IF NOT EXISTS {source.rsplit('.', 1)[0]}")

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
