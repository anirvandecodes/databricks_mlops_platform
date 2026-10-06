-- Sum of amounts (refunds negative) in the 30 days before as_of_date.
coalesce(sum(CASE WHEN t.txn_ts >= date_sub(s.as_of_date, 30) THEN t.amount END), 0)
