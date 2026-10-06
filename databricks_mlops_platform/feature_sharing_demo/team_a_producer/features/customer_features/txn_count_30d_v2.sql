-- Transactions, refunds excluded, in the 30 days before as_of_date.
count(CASE WHEN t.txn_ts >= date_sub(s.as_of_date, 30) AND NOT t.is_refund THEN t.txn_id END)
