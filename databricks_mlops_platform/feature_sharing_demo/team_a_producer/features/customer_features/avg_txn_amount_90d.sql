-- Mean purchase amount (refunds excluded) in the window; 0 if none.
coalesce(avg(CASE WHEN NOT t.is_refund THEN t.amount END), 0)
