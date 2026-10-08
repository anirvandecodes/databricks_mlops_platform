-- customer_features: one row per customer, over all of the customer's transactions.
--
-- Each <feature>.sql file in this folder is one aggregate expression, inserted at
-- ${features}. Expressions use t: the customer's transactions (t.txn_id, t.amount, t.txn_ts).
-- ${transactions} is this environment's source (environments.<env>.sources in the contract).
SELECT
  t.customer_id,
  current_date() AS as_of_date,
  ${features}
FROM ${transactions} t
GROUP BY t.customer_id
