# Cross-team feature sharing — end-to-end demo

A runnable walkthrough of how two teams share features on Databricks: one team
**produces** a governed feature table, another **consumes** it, asks for a change, and the
change ships through a review with approvals. Nobody's model breaks along the way.

- **Team A, producer:** owns `customer_features`, which means its definition, pipeline,
  quality checks and SLA.
- **Team B, consumer:** builds a default-risk model on Team A's features without copying
  any of Team A's logic.

The process behind it is in [`../docs/feature_sharing.md`](../docs/feature_sharing.md).
This folder is the worked example.

```
raw transactions ─▶ Team A pipeline ─▶ UC feature table (PK + TIMESERIES, tags, comments)
                    (contract-driven)          │
                                               ├─▶ Team B: discover ─▶ point-in-time training ─▶ @Champion
                                               │                                   └─▶ score_batch (auto lookup)
   change request ─▶ contract PR ─▶ check_change.py (CI gate) ─▶ approvals ─▶ release
                                    find_feature_consumers (lineage + model deps)
```

## What's where

| Path | Owner | What it is |
|---|---|---|
| `team_a_producer/contracts/customer_features.yaml` | Team A | **Released contract**: schema, meaning, keys, owner, SLA, feature status, changelog |
| `team_a_producer/changes/*.yaml` | Team A | Contract versions used in the demo (stand-ins for PR branches) |
| `team_a_producer/notebooks/01_publish_features.py` | Team A | Computes the features, runs the quality gate, publishes to UC, applies tags |
| `team_b_consumer/feature_config.yaml` | Team B | Team B's pinned dependencies (table, keys, exact columns) |
| `team_b_consumer/notebooks/` | Team B | Discover, train with `FeatureLookup`, `score_batch` |
| `governance/change_requests/CR-*.yaml` | both | The change request: reason, affected consumers, approvals |
| `governance/check_change.py` | platform | CI gate: classify the change, enforce rules, require approvals |
| `governance/notebooks/find_feature_consumers.py` | platform | Who depends on a table or column (model registry + UC lineage) |
| `shared/feature_contract_utils.py` | platform | The rules, as plain functions (used by notebooks, CI and tests) |
| `demo.sh` | — | Runs each step locally (Git stand-in) |
| `../resources/feature-sharing-demo-resource.yml` | platform | The demo's jobs, part of the platform bundle (`../databricks.yml`) |

## Before you start

The demo is part of the platform bundle (`../databricks.yml`): `deploy` deploys the whole
platform, and the demo jobs, schemas and model are per target
(`<catalog_name>.team_a_features_<target>`, …; `catalog_name` defaults to `workspace`).

```bash
cd databricks_mlops_platform/feature_sharing_demo
databricks auth login -p dbc-aef35066-afa2          # once
export DATABRICKS_CONFIG_PROFILE=dbc-aef35066-afa2
pip install pyyaml pytest && pytest tests -q        # the rules, offline
./demo.sh reset && ./demo.sh deploy
./demo.sh run feature_demo_setup                    # schemas + mock data (drops the 3 demo schemas)
```

---

## Act 1 — Team A creates a shared feature table

```bash
./demo.sh run team_a_feature_pipeline
```

- Open `team_a_producer/contracts/customer_features.yaml`. **The contract is the product
  spec:** keys, timestamp key, owner, support channel, refresh, freshness SLA, and a
  definition and status for every feature. It's reviewed in Git like code.
- The pipeline refuses to publish if the contract is invalid, if it promises a feature with
  no logic behind it, or if the quality gate fails (null or duplicate keys, nulls,
  freshness).
- In Catalog Explorer, open `<catalog>.team_a_features_<target>.customer_features`:
  - it's a feature table (PK on `customer_id` plus `as_of_date TIMESERIES`)
  - comments show each feature's definition
  - tags show `feature_owner`, `support_channel`, `contract_version=1.0.0`, `status`
- Access: Team A writes, everyone else reads. The setup task prints the grants.

## Act 2 — Team B discovers and uses it

```bash
./demo.sh run team_b_training        # discover -> train -> batch_score
```

- **Discover** (`01_discover`) finds shared tables by their UC tags, then shows owner,
  SLA, freshness and each column's definition and status. It checks every feature Team B
  pins against what Team A actually publishes.
- **Train** (`02_train_with_lookups`):
  - `create_training_set` with `FeatureLookup(..., timestamp_lookup_key="as_of_date")`
    gives point-in-time correct values, so there's no leakage.
  - `fe.log_model` stores the feature metadata with the model.
  - The model is registered and aliased `@Champion`, and the version is tagged with its
    `feature_dependencies`.
- **Score** (`03_batch_score`): `score_batch` gets **keys only** and looks up the features
  itself. Team B has no copy of Team A's logic, so there's no training/serving skew.
- In Catalog Explorer → Lineage on the feature table, the model and Team B's tables show
  up downstream.

## Act 3 — Team B asks for a change

Team B notices that refunds inflate `txn_count_30d`: risky customers refund a lot, and
the model reads that as healthy activity. Team B can't edit Team A's table, so they file
**CR-001** (`governance/change_requests/CR-001.yaml`; in GitHub this is the
*Feature change request* issue). It asks for two things:
- exclude refunds from the 30-day count, which is a **logic change**
- add a 90-day average purchase amount, which is a **new feature**

Team A checks who depends on the feature before deciding how to ship:

```bash
./demo.sh run feature_consumers_check
```

The report lists `team_b … default_risk_model @Champion` and prints
`affected_consumers: [team_b]`, which is already filled in on CR-001.

## Act 4 — The wrong way, then the right way

**Wrong way: edit the feature in place.**

```bash
./demo.sh check v1.1.0_wrong_in_place CR-001
```

The gate reports **BLOCKED**: `txn_count_30d: logic changed in place — consumers' models
would silently shift. Publish txn_count_30d_v2 and deprecate txn_count_30d`. If this had
shipped, Team B's `@Champion` would have started receiving different values under the same
column name, with no retrain and no warning.

**Right way: version it.**

```bash
diff team_a_producer/contracts/customer_features.yaml team_a_producer/changes/v1.1.0.yaml
./demo.sh check v1.1.0 CR-001
```

The v1.1.0 contract makes three changes:
- adds `txn_count_30d_v2` (refunds excluded)
- adds `avg_txn_amount_90d`
- deprecates `txn_count_30d`, with `sunset_date` and `replaced_by` set

The gate reports `BREAKING` (a deprecation) and lists the required approvals:
**team_a and team_b**. It stays blocked until they approve. Additive-only changes would
need Team A's approval only.

## Act 5 — Approve and release

```bash
./demo.sh approve CR-001 team_a
./demo.sh check v1.1.0 CR-001          # still blocked: team_b missing
./demo.sh approve CR-001 team_b
./demo.sh release v1.1.0 CR-001        # gate passes -> "merge" -> deploy -> publish
./demo.sh run team_b_scoring           # Team B's current @Champion, unchanged
```

- The pipeline adds the two new columns. The old column stays and is tagged
  `status=deprecated`, `sunset_date`, `replaced_by`.
- **Team B's existing model keeps scoring with no change.** It only looks up the columns
  it pinned, which is why producers can release without coordinating every deploy.
- `team_b_training`'s discover step now prints a **WARNING** that `txn_count_30d` is
  deprecated, with its sunset date and replacement. Team B finds out from the platform,
  not from a Slack thread.

## Act 6 — Team B migrates on its own schedule

```bash
diff team_b_consumer/feature_config.yaml team_b_consumer/changes/feature_config.v2.yaml
./demo.sh migrate                      # pin txn_count_30d_v2 + avg_txn_amount_90d, retrain
```

`@Champion` moves to the new version and the deprecation warning disappears. The
training output prints the new holdout AUC against the previous `@Champion`.

On its own, the refund-free count is a much better signal: about 0.73 AUC against
about 0.52 for the count that includes refunds. The full model barely moves, though
(0.769 → 0.764 in the test run), because `txn_amount_sum_30d` already lets it separate
purchases from refunds. Treat this act as showing the **migration mechanics**, not a
model win. In a real CR, Team B would compare challenger and champion before moving the
alias.

## Act 7 — Retire the old feature

```bash
./demo.sh run feature_consumers_check --params column=txn_count_30d,fail_if_active=true
./demo.sh approve CR-002 team_a
./demo.sh check v2.0.0 CR-002
./demo.sh release v2.0.0 CR-002
```

- The consumer gate passes because no aliased model uses `txn_count_30d` any more.
  Lineage still shows past reads, for context. Run the same command before Act 6 and it
  **fails**.
- Removing a feature needs a **major** bump (2.0.0). Because nobody depends on it any
  more, only the producer has to approve. The pipeline drops the column.

Reset for another run: `./demo.sh reset && ./demo.sh deploy && ./demo.sh run feature_demo_setup`.

---

## The rules the gate enforces

| Change | Ship as | Version | Approvals |
|---|---|---|---|
| New feature | new column | minor | producer |
| Logic/meaning change | `<name>_v2` + deprecate old | minor | producer + affected consumers |
| dtype change | `<name>_v2` + deprecate old | minor | producer + affected consumers |
| Key/grain change | new table `<table>_v2` | new contract | producer + affected consumers |
| Remove a feature | only after deprecation, with no active consumers | major | producer (+ any consumer still listed) |
| Description/SLA/owner | metadata edit | patch | producer |

All of these are tested in `tests/test_feature_contract_utils.py`.

## In GitHub

Everything lives in the repo's `.github/` folder:

| File | What it does |
|---|---|
| `workflows/feature_sharing_demo-contract-check.yml` | PR gate: contract-rule tests, `check_change.py` on every changed contract (vs the base branch, with the CR in the PR), bundle validation |
| `workflows/databricks_mlops_platform-bundle-cd-staging.yml` → job `feature_sharing_demo` | Merge to main = release: after the platform deploy, publishes the contract if it changed (then re-scores Team B's `@Champion`), retrains Team B if its config changed, and bootstraps the demo on first run |
| `workflows/feature_sharing_demo-e2e.yml` | Manual (and weekly) run of all seven acts on staging with `demo.sh`, asserting every BLOCKED step, then restores staging |
| `ISSUE_TEMPLATE/feature_change_request.yml` | The *Feature change request* issue (CR-*.yaml as a form) |
| `PULL_REQUEST_TEMPLATE/feature_contract_change.md` | Contract PR checklist (open the PR with `?template=feature_contract_change.md`) |
| `CODEOWNERS` | Team A owns `team_a_producer/`, Team B owns `team_b_consumer/`; replace the handles with real teams |

Setup: the workflows use the repo secrets `DATABRICKS_HOST` / `DATABRICKS_TOKEN` already used
by the platform. Make *Feature contract check* a required status check on `main` and turn
on "Require review from Code Owners".

Then `git` takes the place of `demo.sh`:

| demo.sh | GitHub |
|---|---|
| `check` | the PR's *Feature contract check* job |
| `approve` | a reviewer approves the PR and commits their approval to the CR file |
| `release` | merge to main, then staging CD deploys and runs the pipeline |
| `migrate` | Team B merges its `feature_config.yaml` change; staging CD retrains |
