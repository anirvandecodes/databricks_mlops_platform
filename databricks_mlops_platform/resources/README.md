# ML resource configurations
[(back to project README)](../README.md)

Databricks Asset Bundle definitions for every workflow and ML artifact in the platform. All
five workflows are included and enabled — nothing here needs a TODO resolved before it runs.

## What is defined

| File | Resource | Schedule |
|---|---|---|
| `ml-artifacts-resource.yml` | Registered model + MLflow experiment | — |
| `feature-engineering-workflow-resource.yml` | `write_feature_table_job` | 07:00 UTC (paused) |
| `model-workflow-resource.yml` | `model_training_job` — Train → Validate → **ApprovalGate** → Deploy | 09:00 UTC (paused) |
| `batch-inference-workflow-resource.yml` | `batch_inference_job` — Score → ApplyDecisionRules | 11:00 UTC (paused) |
| `monitoring-resource.yml` | `monitoring_job` — SetupMonitor → DriftCheck → conditional retrain | 18:00 UTC (paused) |
| `deployment-job-resource.yml` | `rollback_job` — on demand, no schedule | — |

Schedules are `PAUSED` by default so a demo environment does not accumulate unattended runs.
The cron expressions document the intended cadence; unpause per environment when going live.
The 07:00 → 09:00 → 11:00 → 18:00 ordering is deliberate: features land before training,
training before scoring, scoring before drift evaluation.

## Environment configuration

There are no per-environment resource files. One set of definitions is parameterised by the
target variables in [`../databricks.yml`](../databricks.yml):

| Variable | Purpose |
|---|---|
| `catalog_name` / `schema_name` | Where this environment's assets live |
| `model_name` | Registered model name |
| `approval_required` | **The governance switch.** `true` in prod. |
| `psi_warn_threshold` / `psi_retrain_threshold` | Drift sensitivity |

Deploy a specific target:

```bash
databricks bundle validate -t prod
databricks bundle deploy -t prod
```

## Changing a resource

Edit the YAML and redeploy. Job `base_parameters` are resolved at **deploy** time, so
passing `--var` to `bundle run` alone will not change a job's behaviour — the variable must
be set on `bundle deploy`.

To change compute, edit the `environments` block. All jobs use serverless with dependencies
from `../requirements.txt`.

## Why the training workflow is ordered as it is

```
Train  →  ModelValidation  →  ApprovalGate  →  ModelDeployment
```

Each task can block the next but never skip it. `Train` registers a **challenger** and does
not touch the `champion` alias, so a training run cannot change what production serves.
Promotion happens only in `ModelDeployment`, which is unreachable unless validation passed
and the approval gate allowed it.

Adding a task between `ApprovalGate` and `ModelDeployment` is safe. Adding one that moves an
alias outside `ModelDeployment` would bypass the gate — don't.
