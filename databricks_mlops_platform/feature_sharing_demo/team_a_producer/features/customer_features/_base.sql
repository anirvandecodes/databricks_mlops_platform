-- customer_features: one row per customer per weekly snapshot (as_of_date).
--
-- Each <feature>.sql file in this folder is one aggregate expression, inserted at
-- ${features}. Expressions can use:
--   s  the snapshot row: s.customer_id, s.as_of_date
--   t  transactions in the 90 days strictly before s.as_of_date (point-in-time correct)
--
-- Changing this file changes every feature, so the PR check blocks it: a new grain, window
-- or source ships as a new table.
WITH snapshots AS (
  SELECT c.customer_id, d.as_of_date
  FROM (SELECT DISTINCT customer_id FROM ${catalog}.${raw_schema}.transactions) c
  CROSS JOIN (
    SELECT explode(sequence(DATE'2026-01-04', date_add(max(to_date(txn_ts)), 1), INTERVAL 7 DAYS)) AS as_of_date
    FROM ${catalog}.${raw_schema}.transactions) d
)
SELECT s.customer_id, s.as_of_date,
  ${features}
FROM snapshots s
LEFT JOIN ${catalog}.${raw_schema}.transactions t
  ON t.customer_id = s.customer_id
 AND t.txn_ts <  CAST(s.as_of_date AS TIMESTAMP)
 AND t.txn_ts >= CAST(date_sub(s.as_of_date, 90) AS TIMESTAMP)
GROUP BY s.customer_id, s.as_of_date
