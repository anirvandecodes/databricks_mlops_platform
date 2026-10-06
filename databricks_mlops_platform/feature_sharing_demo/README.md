# Cross-team feature sharing: changing a shared feature through a PR

**Team A** owns a feature table. **Team B** trains a model on it. Team A changes the table
with a pull request, and a check on the PR asks the live model registry: *does any model
read a column this PR changes?* If one does, the PR is blocked.

| | Team A's PR | Check result |
|---|---|---|
| ✅ Happy path | Adds a new feature | **OK to merge**: no model reads a column that doesn't exist yet |
| ❌ Not happy path | Changes how an existing feature is calculated | **BLOCKED**: Team B's `@champion` reads it and would silently get different numbers |

```
edit contract on a branch → PR → Feature contract check ──► no model affected → ✅ review → merge → staging publishes
                                                         └─► a model reads it  → ❌ blocked, PR comment names the model
```

## How it works

- **The contract** (`team_a_producer/contracts/customer_features.yaml`) describes the table:
  keys, owner, and each feature's type, definition and `logic_version`. Team A's pipeline
  (`team_a_producer/notebooks/01_publish_features.py`) builds the UC feature table from it.
  The SQL for each feature is in that notebook's `FEATURE_LOGIC`.
- **Team B** lists the columns it reads in `team_b_consumer/feature_config.yaml`. When it
  trains, the model version is tagged `feature_dependencies` with those columns.
- **The check** (`governance/check_change.py`) diffs the PR's contract against `main`, finds
  every model version with an alias (`@champion`, …) in the catalog, and reads its
  `feature_dependencies` tag. It runs for staging and prod and comments on the PR.
- **Approval** is a code-owner review (`.github/CODEOWNERS`). Nothing is written into YAML.

## One-time setup

1. Repo secrets `DATABRICKS_HOST` / `DATABRICKS_TOKEN`. The token's identity must be able to
   read every model in the catalog.
2. Branch protection on `main`: required check **Downstream impact (live model registry)**,
   and **Require review from Code Owners**.
3. Staging has Team A's table and Team B's `@champion`. The first staging CD run does this,
   or run it by hand:
   ```bash
   cd databricks_mlops_platform && export DATABRICKS_CONFIG_PROFILE=dbc-aef35066-afa2
   databricks bundle deploy -t staging
   databricks bundle run feature_demo_setup -t staging     # drops and recreates the demo schemas
   databricks bundle run team_a_feature_pipeline -t staging
   databricks bundle run team_b_training -t staging
   ```

## Case 1: happy path, add a feature

```bash
git switch main && git pull && git switch -c team-a/add-avg-txn-amount-90d
```

In `customer_features.yaml`, set `version: 1.1.0` and add:

```yaml
features:
  - name: avg_txn_amount_90d
    dtype: double
    definition: Mean purchase amount (refunds excluded) in the 90 days before as_of_date (0 if none).
    logic_version: 1
    status: active
    since: 1.1.0

changelog:
  - version: 1.1.0
    date: "2026-10-06"
    change: Added avg_txn_amount_90d.
```

(`FEATURE_LOGIC` already has the SQL for it.)

```bash
git commit -am "Add avg_txn_amount_90d" && git push -u origin HEAD && gh pr create --fill
```

Result: ✅ **OK to merge**. After merging, staging CD (about 10 minutes) adds the column and
re-scores with Team B's `@champion`, unchanged.

## Case 2: not happy path, change a feature in place

```bash
git switch main && git pull && git switch -c team-a/txn-count-exclude-refunds
```

- In `customer_features.yaml`, for `txn_count_30d`: change `definition` to `Number of
  purchase transactions (refunds excluded) in the 30 days before as_of_date.`, set
  `logic_version: 2`, bump `version` to `2.0.0`, and add a changelog entry.
- In `01_publish_features.py`: `"txn_count_30d": f"count(CASE WHEN {W30} AND NOT t.is_refund THEN t.txn_id END)",`

```bash
git commit -am "Exclude refunds from txn_count_30d" && git push -u origin HEAD && gh pr create --fill
```

Result: ❌ **BLOCKED**

```
BLOCKED — this would break downstream:
  x workspace.team_b_ml_staging.default_risk_model v1 @champion (team_b) reads txn_count_30d: logic/definition changed in place

  To ship this without breaking anyone: add the new logic as txn_count_30d_v2 (a new column) and leave txn_count_30d as it is,
  or wait until no live model reads txn_count_30d.
```

The way out is case 1: add `txn_count_30d_v2` as a new column. Team B switches to it in its
own PR to `feature_config.yaml`.

## The rules

| Change | Version | Blocked when |
|---|---|---|
| New feature | minor | never |
| Deprecate a feature | minor | never |
| Change a feature's definition or logic | major | a live model reads it |
| Remove a feature | major | a live model reads it |
| Change a dtype, keys or table name | — | always (ship `<name>_v2` or a new table) |
| Description, owner, SLA | patch | never |

Tested in `tests/test_feature_contract_utils.py` (`pytest tests -q`). To run the check
locally:

```bash
git show origin/main:databricks_mlops_platform/feature_sharing_demo/team_a_producer/contracts/customer_features.yaml > /tmp/base.yaml
python governance/check_change.py --base /tmp/base.yaml --proposed team_a_producer/contracts/customer_features.yaml \
  --var catalog=workspace --var producer_schema=team_a_features_staging --var raw_schema=feature_demo_raw_staging
```

**Not covered:** readers that aren't aliased UC models (dashboards, ad hoc jobs, other
catalogs), and `FEATURE_LOGIC` edits made without changing the contract. Code-owner review
catches those.
