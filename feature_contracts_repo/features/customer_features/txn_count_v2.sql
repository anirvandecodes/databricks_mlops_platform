-- Total number of purchases, excluding refunds.
count(CASE WHEN NOT t.is_refund THEN t.txn_id END)
