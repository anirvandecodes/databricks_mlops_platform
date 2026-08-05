# ML project

The ML code and resource definitions for the credit-risk reference workload.

See the [repository README](../README.md) for the platform overview and
[docs/CUJ_DEMO.md](../docs/CUJ_DEMO.md) for a walkthrough with real outputs.

## Where things live

| Path | Purpose |
|---|---|
| `databricks.yml` | Bundle root: three targets, all environment differences as variables |
| `platform_utils/` | Shared platform layer (naming, promotion, audit, metrics) |
| `feature_engineering/` | Feature transforms — pure functions, unit tested offline |
| `training/Train.py` | Trains LightGBM, registers a **challenger** (never touches champion) |
| `validation/` | Quality thresholds and the decision threshold — risk-owned parameters |
| `deployment/approval/` | Stage-2 model gate |
| `deployment/model_deployment/` | Promotion and rollback |
| `deployment/batch_inference/` | Alias-resolved scoring plus inference logging |
| `monitoring/` | Monitor setup, PSI drift check, gated retraining |
| `decision_layer/` | Business policy as Unity Catalog SQL functions |
| `resources/` | The five workflows, declaratively |
| `tests/` | `unit/` runs offline; `integration/` needs a workspace |

## Local development

The dev target allows direct deploys from a local IDE — this is the fast inner loop:

```bash
databricks bundle deploy -t dev
databricks bundle run model_training_job -t dev
```

Re-run a single task without repeating the whole workflow:

```bash
databricks bundle run model_training_job -t dev --only ApprovalGate
```

Staging and prod do not accept local deploys; changes arrive there only through CI/CD.

## Changing behaviour

| To change | Edit | Retraining needed? |
|---|---|---|
| Feature logic | `feature_engineering/features/credit_features.py` | yes |
| Model quality bar | `validation/validation.py` | no — the next run enforces it |
| Decision threshold | `validation/validation.py` (`DECISION_THRESHOLD`) | no |
| Risk bands / credit limits | `decision_layer/policy.py` | **no** — policy is decoupled |
| Drift thresholds | `databricks.yml` target variables | no |
| Approval requirement | `databricks.yml` (`approval_required`) | no |

Note that job `base_parameters` are baked at **deploy** time. Changing a variable therefore
requires `bundle deploy`; passing `--var` to `bundle run` alone will not take effect.

## Adding another model project

The platform layer is workload-agnostic. To onboard a second model:

1. Add a feature module beside `credit_features.py` exposing `compute_features_fn`.
2. Point `training/Train.py` at it and set the label column.
3. Set thresholds in `validation/validation.py`.
4. Replace the policy functions in `decision_layer/policy.py`, or drop the decision task.
5. Set `model_name` per target in `databricks.yml`.

`platform_utils/` needs no changes — promotion, audit, rollback and drift are generic.
