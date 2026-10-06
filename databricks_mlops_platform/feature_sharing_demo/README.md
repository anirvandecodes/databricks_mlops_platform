# Cross-team feature sharing: changing a shared feature through a PR

**Team A** owns a feature table. **Team B** trains a model on it. Team A changes the table
with a pull request, and a check on the PR decides whether the change is safe to ship.

The rule the check enforces is the one feature platforms use: **released features are
immutable.** A feature's SQL and type never change after release. New logic ships as a new
feature (`<name>_v2`), the old one is deprecated, and it's removed only once nothing in Unity
Catalog depends on it.

| | Team A's PR | Check result |
|---|---|---|
| 1 ✅ Happy path | Adds a new feature | **OK to merge** |
| 2 ❌ Not happy path | Changes an existing feature's SQL | **BLOCKED**: released features are immutable, and the check lists who it would have broken (Team B's `@champion` and its training and scoring jobs) |
| 3 ✅ Right way | Adds the new logic as `txn_count_30d_v2` and deprecates `txn_count_30d` | **OK to merge**: Team B keeps reading the old column, unchanged, and moves when it's ready |

## Layout

| Path | Owner | What it is |
|---|---|---|
| `team_a_producer/contracts/customer_features.yaml` | Team A | The **contract**: what the table promises — keys, owner, SLA, and each feature's type, definition, status and lifecycle |
| `team_a_producer/features/customer_features/<feature>.sql` | Team A | **How** each feature is computed: one SQL aggregate expression per feature |
| `team_a_producer/features/customer_features/_base.sql` | Team A | The query skeleton the expressions go into (grain, point-in-time join, window) |
| `team_a_producer/notebooks/01_publish_features.py` | Team A | Runs `_base.sql` with every expression, runs the quality gate, publishes the UC feature table, writes comments and tags |
| `team_b_consumer/feature_config.yaml` | Team B | The exact columns Team B reads; it trains with `FeatureLookup` + `fe.log_model` |
| `governance/check_change.py` | Platform | **The PR check** (below) |
| `governance/dependencies.py` | Platform | What depends on a table, from Unity Catalog's own records; and the SQL compile check |
| `shared/feature_contract_utils.py` | Platform | The rules, as plain functions (unit-tested) |
| `governance/notebooks/find_feature_consumers.py` | Platform | The same dependency lookup as a job: "who uses my table / column?" |

## The PR check

`.github/workflows/feature_sharing_demo-contract-check.yml` runs on every PR that touches the
demo. For each feature table whose contract **or SQL** changed, `check_change.py` compares
`main` with the PR, once for staging and once for prod:

| Step | Question | Blocks when |
|---|---|---|
| 1 | Are the contract and SQL valid, one SQL file per feature? | a field or file is missing, or they don't match |
| 2 | What changed? | — (lists added / deprecated / removed / changed / metadata) |
| 3 | Is it allowed? | a released feature's SQL or type changed; an active feature was removed; keys or `_base.sql` changed; the version bump or changelog entry is wrong |
| 4 | Does the SQL compile? | `EXPLAIN` of the full feature query fails (only when feature SQL was added or changed) |
| 5 | What depends on it? | something still reads a feature this removes; or a source can't be read |

Step 5 runs only when a change affects existing readers. It uses Unity Catalog's records:

| What | Source | Counted when |
|---|---|---|
| **Models** | UC lineage lists every model version logged with `fe.log_model` from the table, in any catalog; each version's `feature_spec.yaml` gives the exact columns | The version has an alias (`@champion`, …) **or** a serving endpoint serves it. Unknown columns count as every column. |
| **Jobs, pipelines, dashboards** | `system.access.column_lineage`: the columns each one read **in its latest run**, last 30 days | Always, except the producer (anything that writes the table) |
| **Ad hoc notebooks and queries** | same | Never blocks: listed as warnings |

If a source can't be read, the PR is blocked: an unknown answer isn't a yes. Transient API
errors are retried first. Every step is logged live in the CI log; the PR comment has the
report.

Approval is a code-owner review (`.github/CODEOWNERS`).

## The rules

| Change | How | Version |
|---|---|---|
| New feature | contract entry + `<feature>.sql` | minor |
| New logic for an existing feature | a new feature `<name>_v2`; deprecate `<name>` | minor |
| Deprecate a feature | `status: deprecated`, `sunset_date`, `replaced_by` | minor |
| Remove a feature | only once deprecated **and** nothing depends on it (warns if before `sunset_date`) | major |
| Definition text, description, owner, SLA | metadata | patch |
| Change a released feature's SQL or type | **never** — ship `<name>_v2` | — |
| Change keys, `_base.sql` (grain, window, source) or the table name | **never** — ship a new table | — |

SQL comments and whitespace aren't changes. Every version needs a changelog entry.

## Setup

1. Repo secrets `DATABRICKS_HOST` + `DATABRICKS_TOKEN`, or, for production, a service
   principal (`DATABRICKS_CLIENT_ID` / `DATABRICKS_CLIENT_SECRET`). The identity must read
   every model in the catalog, read `system.access`, and use a SQL warehouse
   (`DATABRICKS_WAREHOUSE_ID`, or the first serverless one).
2. Branch protection on `main`: required check **Downstream impact (live model registry)** and
   **Require review from Code Owners**. Without it the check only advises.
3. Staging has Team A's table and Team B's `@champion` (the first staging CD run does this):
   ```bash
   cd databricks_mlops_platform && export DATABRICKS_CONFIG_PROFILE=dbc-aef35066-afa2
   databricks bundle deploy -t staging
   databricks bundle run feature_demo_setup -t staging     # drops and recreates the demo schemas
   databricks bundle run team_a_feature_pipeline -t staging
   databricks bundle run team_b_training -t staging
   ```

CI installs pinned versions from `requirements-ci.txt`; upgrade them deliberately.

## Making a change

**Add a feature** (case 1 and 3): add it to `features:` in the contract (with `since` = the
new version), add `team_a_producer/features/customer_features/<name>.sql`, bump the minor
version, add a changelog entry, open a PR.

```yaml
  - name: avg_txn_amount_90d
    dtype: double
    definition: Mean purchase amount (refunds excluded) in the 90 days before as_of_date (0 if none).
    status: active
    since: 1.1.0
```
```sql
-- avg_txn_amount_90d.sql
coalesce(avg(CASE WHEN NOT t.is_refund THEN t.amount END), 0)
```

**Change a feature's logic** (case 3, not case 2): add `<name>_v2` as above and, in the same
PR, deprecate `<name>` (`status: deprecated`, `sunset_date`, `replaced_by: <name>_v2`).
Consumers move to `_v2` in their own PRs; Team B's discover step warns them until they do.

**Remove a deprecated feature**: delete it from the contract and delete its `.sql` file,
bump the major version. The check blocks it while anything still reads it.

Run the check locally:

```bash
cd databricks_mlops_platform/feature_sharing_demo
pip install -r requirements-ci.txt && pytest tests -q
git worktree add /tmp/base origin/main
python governance/check_change.py \
  --base /tmp/base/databricks_mlops_platform/feature_sharing_demo/team_a_producer/contracts/customer_features.yaml \
  --proposed team_a_producer/contracts/customer_features.yaml \
  --var catalog=workspace --var producer_schema=team_a_features_staging --var raw_schema=feature_demo_raw_staging
```

**Not covered:** readers outside Unity Catalog (exports, external engines reading the files),
models that don't use `fe.log_model` (they show up only as the job that read the table), and
readers so new that lineage hasn't caught up (minutes). Immutability makes the first and last
safe for changes; they matter only for removal.
