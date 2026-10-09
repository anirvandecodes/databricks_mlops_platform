<!-- Feature contract PRs: open with ?template=feature_contract_change.md -->
## Summary

## Feature table change (delete if no contract or feature SQL changed)
- Table: `<table>` `<old version>` → `<new version>`
- Requested in: #issue (if a consumer asked for it)

Checklist:
- [ ] New table: a new `features/<table>/` folder (contract + `_base.sql` + one `<name>.sql` per feature), with your team in CODEOWNERS
- [ ] The contract is valid ODCS v3.0.1 (the check validates it against the JSON schema)
- [ ] `servers:` has dev, staging and prod (catalog, schema, sources); no table moved in place
- [ ] New features: a `schema` property **and** `features/<table>/<name>.sql`
- [ ] No released feature's SQL or type changed — new logic ships as `<name>_v2`, with `<name>` deprecated
- [ ] Deprecated features have `sunsetDate` and `replacedBy`
- [ ] `roles` lists every team that should read the table (removing a team revokes its access)
- [ ] Version bumped (patch: metadata, access granted · minor: new or deprecated feature, access revoked · major: removal) + changelog entry
- [ ] *Feature contract check* is green
- [ ] Announced in the contract's `support` channel after release
