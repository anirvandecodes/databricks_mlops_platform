<!-- Feature contract PRs: open with ?template=feature_contract_change.md -->
## Summary

## Feature contract changes (delete if no contract file changed)
- Change request: CR-___ / #issue
- Contract: `<table>` `<old version>` → `<new version>`
- Change kind reported by the contract check: none / metadata / additive / breaking

Checklist:
- [ ] Contract version bumped (patch: metadata · minor: new/deprecated feature · major: removal)
- [ ] Changelog entry for the new version
- [ ] No in-place logic/dtype change — changed logic ships as `<name>_v2`
- [ ] Deprecated features have `sunset_date` and `replaced_by`
- [ ] Ran `find_feature_consumers` and filled `affected_consumers` in the CR
- [ ] Breaking: every affected consumer team approved in the CR (CI enforces this)
- [ ] Announced in the contract's `support_channel` after release
