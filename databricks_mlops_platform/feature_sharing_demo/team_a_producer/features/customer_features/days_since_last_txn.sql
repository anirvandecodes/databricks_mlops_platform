-- Days since the last transaction in the window; 999 if none.
coalesce(datediff(s.as_of_date, max(to_date(t.txn_ts))), 999)
