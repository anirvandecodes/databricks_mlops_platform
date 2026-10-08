-- Days since the customer's last transaction; 999 if none.
coalesce(datediff(current_date(), max(to_date(t.txn_ts))), 999)
