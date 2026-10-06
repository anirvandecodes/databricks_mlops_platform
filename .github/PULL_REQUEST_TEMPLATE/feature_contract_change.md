<!-- Feature contract PRs: open with ?template=feature_contract_change.md -->
## Summary

## Feature table change (delete if no contract or feature SQL changed)
- Table: `<table>` `<old version>` → `<new version>`
- Requested in: #issue (if a consumer asked for it)

Checklist:
- [ ] New features: contract entry **and** `team_a_producer/features/<table>/<name>.sql`
- [ ] No released feature's SQL or type changed — new logic ships as `<name>_v2`, with `<name>` deprecated
- [ ] Deprecated features have `sunset_date` and `replaced_by`
- [ ] Version bumped (patch: metadata · minor: new or deprecated feature · major: removal) + changelog entry
- [ ] *Feature contract check* is green
- [ ] Announced in the contract's `support_channel` after release
