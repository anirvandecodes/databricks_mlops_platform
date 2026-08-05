# MLOps Reference Platform

An end-to-end, governed MLOps platform on Databricks, built around one claim:

> **No model reaches production without passing an automated quality bar and a named
> human's approval — and any model can be removed from production in seconds.**

The reference workload is a credit-risk default-probability model (LightGBM on the public
UCI Credit-G dataset), chosen because credit risk has the strictest governance
requirements. The platform itself is workload-agnostic.

- **[docs/CUJ_DEMO.md](docs/CUJ_DEMO.md)** — guided walkthrough with real outputs
- **[docs/GITHUB_ACTIONS_SETUP.md](docs/GITHUB_ACTIONS_SETUP.md)** — CI/CD configuration

---

## What makes this different from a training pipeline

| Capability | How it works |
|---|---|
| **Two-stage gating** | Stage 1 (code): PR review, unit tests, bundle validation. Stage 2 (model): production CD pauses on a GitHub environment approval *after* training and validation, so the reviewer decides with the candidate's metrics in front of them. Approving records `approval_status` on that specific version in Unity Catalog; the in-pipeline gate re-checks the tag before any alias moves. |
| **Alias-based promotion** | Inference resolves `models:/<model>@champion` at run time. Promotion moves an alias; nothing is redeployed. |
| **Seconds-long rollback** | The same alias operation in reverse. Deliberately *not* gated on approval — a gate must not prolong an incident. |
| **Audit evidence** | Every promotion and rollback writes an immutable JSON package to a UC Volume plus a queryable Delta row: metrics, training-data version, approver, timestamp. |
| **Governed drift metrics** | PSI registered as a Unity Catalog function, so every team uses one audited formula. Integration tests assert the SQL matches the Python implementation. |
| **Gated auto-retraining** | Drift triggers model *building*, never model *promotion*. Retrained models enter as challengers and face the same gate. |
| **Decoupled decision layer** | The model outputs a probability; business rules (eligibility, risk bands, limit caps) are separate UC SQL functions. Policy changes ship without retraining. |

## Pipeline

```
feature engineering  →  train  →  validate  →  APPROVAL GATE  →  promote  →  score  →  decide
      (07:00)          (09:00)                                                (11:00)
                                        ↑                                        │
                                   blocks here                                   ↓
                                   in production                          inference log
                                        │                                        │
                                   retraining  ←──  drift breach  ←────  monitoring (18:00)
```

## Layout

```
databricks_mlops_platform/
├── databricks.yml              # 3 targets (dev/staging/prod); all env differences are variables
├── platform_utils/             # shared platform layer — the reusable part
│   ├── naming.py               #   asset addressing; one place to change the UC layout
│   ├── promotion.py            #   approval gate, promotion, rollback
│   ├── audit.py                #   evidence packages
│   ├── metrics.py              #   PSI (Python + UC SQL function)
│   └── task_values.py          #   safe task-value access across single-task re-runs
├── feature_engineering/        # transforms — pure functions, unit tested
├── training/                   # LightGBM training; registers a CHALLENGER only
├── validation/                 # quality thresholds; risk-owned parameters
├── deployment/
│   ├── approval/               #   Stage-2 model gate
│   ├── model_deployment/       #   promotion + rollback
│   └── batch_inference/        #   alias-resolved scoring + inference logging
├── monitoring/                 # monitor setup, PSI drift check, gated retraining
├── decision_layer/             # business policy as UC SQL functions
├── resources/                  # 5 workflows as declarative YAML
├── scripts/bootstrap_uc.py     # one-time UC setup (idempotent)
└── tests/{unit,integration}/   # 83 offline + 19 workspace tests
```

## Quick start

```bash
# 1. One-time UC setup (needs CREATE SCHEMA on the catalog — may require an admin)
python databricks_mlops_platform/scripts/bootstrap_uc.py --profile <profile>

# 2. Deploy and run
cd databricks_mlops_platform
databricks bundle deploy -t dev
databricks bundle run write_feature_table_job -t dev
databricks bundle run model_training_job -t dev
databricks bundle run batch_inference_job -t dev
databricks bundle run monitoring_job -t dev

# Roll back if needed
databricks bundle run rollback_job -t dev
```

## Testing

Three tiers, matching where each class of defect actually surfaces:

```bash
pytest tests/unit -q          # offline; transforms, PSI, gate logic, policy rules
pytest tests/integration -q   # real UC; needs MLOPS_TEST_PROFILE
```

The third tier is the staging integration run in CI, which executes the whole pipeline on
every pull request. That tier caught defects the others structurally cannot — an MLflow API
rename, a baseline/inference schema mismatch, and an SDK argument valid on create but
rejected on update.

**Local Spark note:** where `databricks-connect` is installed, its `pyspark` refuses local
sessions. The `spark` fixture prefers a Connect session against serverless and falls back to
local Spark, so both dev machines and CI work:

```bash
MLOPS_TEST_PROFILE=<profile> pytest tests/unit -q
```

## Governance model

Environment separation uses one catalog with a schema per environment
(`payments_dev` / `payments_staging` / `payments_prod`), enforced by schema-level grants.

Catalog-per-environment is the stronger posture and is preferred where the metastore allows
`CREATE CATALOG`. Because both layouts are addressed through the same
`catalog_name` / `schema_name` variables, migrating is a variable change per target — no
pipeline code moves.

| | dev | staging | prod |
|---|---|---|---|
| Deploy path | local IDE | CI on merge to `main` | CD on push to `release/**` |
| Approval required | no | no | **yes** |
| Human write access | yes | no | no |

Production releases are namespaced branches — `release/2026-08-cutover` rather than one
long-lived `release` — so the branch name records which release a given promotion belonged
to. The `production` GitHub environment restricts deployments to that pattern and requires a
named reviewer.

## Not implemented

Stated plainly so scope is clear: distributed hyperparameter tuning, real-time serving and
the online feature store, and FinOps tagging/budgets. Approval is captured as a GitHub
environment review plus a Unity Catalog tag rather than through a developer portal — the
control is enforced; the portal integration is not built.

**Lakehouse Monitoring is unavailable on Databricks Free Edition** — the quality-monitors API
is not served there at all, even to a workspace admin. `SetupMonitor` detects that and exits
`MONITOR_UNSUPPORTED` instead of failing the pipeline. Drift detection is unaffected:
`DriftCheck` computes PSI from the UC function against the baseline table, so the
drift-triggers-retraining path still works. Only the managed profile/drift metric tables are
absent.
