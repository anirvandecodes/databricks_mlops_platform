# Monitoring

Drift detection and gated retraining. Enabled by default — there are no TODOs to complete.

| File | Purpose |
|---|---|
| `SetupMonitor.py` | Registers the PSI Unity Catalog function and attaches a data profiling monitor to the inference log. Idempotent. |
| `JoinGroundTruth.py` | Backfills matured outcomes into the log's `ground_truth` column. Idempotent. |
| `label_join.py` | The merge logic behind that backfill, unit tested offline. |
| `DriftCheck.py` | Computes PSI per monitored feature against the training baseline and decides whether retraining is warranted. |

## Why the label join is a separate step

Batch scoring cannot know the outcome of a credit decision, so it writes `ground_truth` as a
typed null placeholder. The monitor's **model-quality** metrics — accuracy, precision, recall,
confusion matrix — all derive from that column, and an all-null label column produces *empty
panels rather than an error*, while the data-quality half (column profiles, drift) still looks
healthy. That makes the omission easy to miss, which is why the backfill is an explicit task
rather than an implicit side effect of scoring.

The merge only fills rows whose label is still null, so it never revises a recorded outcome —
settled labels are audit evidence. Rows awaiting outcomes are expected, and the task reports
label coverage so "how much of the log is labelled" is visible rather than inferred.

## Running it

```bash
databricks bundle run monitoring_job -t dev
```

The inference log must exist first, so run `batch_inference_job` at least once — a monitor
cannot attach to a table that does not yet exist.

## How drift is measured

PSI (Population Stability Index) is the metric credit-risk teams govern on, and it is not a
built-in data profiling metric. It is implemented twice, deliberately:

- `platform_utils/metrics.py` — a pure Python function, unit tested offline.
- The same formula registered as a **Unity Catalog SQL function**, so every team computes
  drift with one audited definition.

`tests/integration/test_uc_functions.py` asserts the two agree, which is what stops the
tested logic from drifting away from the deployed logic.

Verdicts (thresholds are bundle variables, tunable per environment):

| PSI | Verdict | Action |
|---|---|---|
| < 0.10 | STABLE | none |
| 0.10 – 0.25 | WARN | recorded, no action |
| ≥ 0.25 | RETRAIN | triggers the training workflow |

## The governance rule

Drift triggers model **building**, never model **promotion**. A retrained model registers as
a challenger and faces the same approval gate as any other candidate. Automated drift
response must not become an unreviewed path into production.

## Gotchas

The drift baseline and the inference log must use the same label column name
(`ground_truth`). Data profiling rejects a mismatch with `label_col cannot be found`.

**`prediction` must hold a class, not a probability.** With a classification problem type the
monitor compares `prediction_col` against `label_col` for equality, so logging the raw
probability yields accuracy `0.0` and an all-zero confusion matrix — *without any error*, since
0.42 is a perfectly valid double. Batch inference therefore thresholds at `DECISION_THRESHOLD`
(imported from `validation`, so the class scored is the class the model was approved on) and
keeps the probability as `prediction_score` for score-distribution analysis.

Both failure modes here are silent: an unlabelled log and a probability-valued `prediction`
each leave the data-quality half of the dashboard looking perfectly healthy.

This platform monitors batch inference tables directly. Real-time serving inference tables
require unpacking before a monitor can attach.
