-- Sum of all transaction amounts (refunds negative).
coalesce(sum(t.amount), 0)
