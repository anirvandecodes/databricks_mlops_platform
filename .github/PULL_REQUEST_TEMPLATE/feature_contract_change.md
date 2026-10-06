<!-- Feature contract PRs: open with ?template=feature_contract_change.md -->
## Summary

## Feature contract change (delete if no contract file changed)
- Contract: `<table>` `<old version>` → `<new version>`
- Requested in: #issue (if a consumer asked for it)

Checklist:
- [ ] Version bumped (patch: metadata · minor: new or deprecated feature · major: changed or removed feature)
- [ ] Changelog entry for the new version
- [ ] Pipeline logic (`FEATURE_LOGIC`) updated for every new or changed feature
- [ ] New logic for an existing feature ships as `<name>_v2` unless no live model reads it
- [ ] *Feature contract check* is green (it fails if a live model reads a column this PR changes or removes)
- [ ] Announced in the contract's `support_channel` after release
