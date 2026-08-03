# MLOps Platform — Customer Demo Script

A step-by-step walkthrough of the end-to-end platform, with the narrative to use at each
step. Every command below was run against a live workspace while building this; the
outputs quoted are real.

**Total runtime:** ~25 minutes of pipeline execution. Budget 45 minutes with narration,
or run the setup phase beforehand and demo only Acts 2–4 in ~20 minutes.

---

## The story in one sentence

Any team can train a model. What a regulated credit-risk platform has to prove is that
**no model can reach production without passing an automated quality bar and a named
human's approval — and that any model can be taken out of production in seconds.**

Everything else in the demo supports that sentence.

---

## Before the demo

### 1. Confirm the environment schemas exist (one-time)

```bash
python scripts/bootstrap_uc.py --profile <profile> \
  --catalog paypay_demo_catalog --prefix payments
```

Idempotent: it creates `payments_dev`, `payments_staging`, `payments_prod` plus the audit
Volume if missing, then verifies each is writable. Re-running it is the quickest way to check
the environment is still sound.

Requires `CREATE SCHEMA` on the catalog. If it reports `PERMISSION_DENIED`, have a workspace
admin run the `CREATE SCHEMA` statements it prints.

### 2. Deploy and prime dev

```bash
cd databricks_mlops_platform
databricks bundle deploy -t dev
databricks bundle run write_feature_table_job -t dev    # ~2 min
databricks bundle run model_training_job -t dev         # ~4 min
databricks bundle run batch_inference_job -t dev        # ~3 min
```

You now have a champion model, a scored batch, decisions, and an audit trail — a system
that already looks alive, which is a much better starting point than an empty workspace.

### 3. Have these tabs open

1. **Workflows** — the five jobs
2. **Models** → `credit_risk_model` → **Versions** (aliases and tags are the centrepiece)
3. A **SQL editor** with the queries from Act 4 pasted in
4. The repo in an editor, on `validation/validation.py`

---

## Act 1 — Infrastructure as code (3 min)

**Point:** the platform is a reviewable artifact, not a collection of clicked-together
jobs.

Show `databricks.yml`. Three targets, one code path. Everything environment-specific is a
variable:

```yaml
dev:      schema_name: payments_dev      approval_required: "false"
staging:  schema_name: payments_staging  approval_required: "false"
prod:     schema_name: payments_prod     approval_required: "true"
```

> "The code that will run in production is the code that ran in staging. The only thing
> that differs is configuration — and the configuration is in version control, so
> tightening a governance rule is a pull request with an author and a reviewer, not a
> console setting someone changed on a Tuesday."

Then validate all three targets live:

```bash
for t in dev staging prod; do databricks bundle validate -t $t; done
```

> "A bad production variable fails here, on the pull request, not at release time."

---

## Act 2 — The governance chain (8 min) ★ the core of the demo

### 2a. Show the chain

Open `resources/model-workflow-resource.yml`. Four tasks, strictly ordered:

```
Train  →  ModelValidation  →  ApprovalGate  →  ModelDeployment
```

> "Each task can block the next but never skip it. There is no path from 'a model was
> trained' to 'a model serves traffic' that bypasses validation and approval."

Point out what `Train` does **not** do:

```python
register_challenger(names.model_name, model_version)
print("@champion is unchanged.")
```

> "Training registers a *candidate*. Nothing that serves traffic is touched. That
> separation is what makes the next step meaningful."

### 2b. Run it in dev — the happy path

```bash
databricks bundle run model_training_job -t dev
```

Real output:

```
Task Train:            models:/paypay_demo_catalog.payments_dev.credit_risk_model/6
Task ModelValidation:  PASSED
Task ApprovalGate:     AUTO_APPROVED
Task ModelDeployment:  PROMOTED:6
```

> "In dev the gate is deliberately open — `approval_required=false` — because data
> scientists need to iterate without asking permission. Same code, different config."

### 2c. Now the production behaviour ★ the moment that sells it

```bash
databricks bundle deploy -t prod
databricks bundle run model_training_job -t prod
```

The job **fails**, by design:

```
======================================================================
PROMOTION BLOCKED — approval required in environment 'prod'.
======================================================================
Model  : paypay_demo_catalog.payments_prod.credit_risk_model
Version: 5

No 'approval_status' tag on version 5. A reviewer must set
approval_status=approved on the model version to authorise promotion.

Production keeps serving champion v4 until then.
======================================================================
```

Switch to the Models tab and show the aliases:

| Alias | Version | Meaning |
|---|---|---|
| `@champion` | **v4** | still serving production traffic |
| `@challenger` | **v5** | trained, validated, quarantined |

> "This is the whole design in one screen. The model trained fine and passed validation —
> then stopped. Production is still serving the last approved model. Nobody had to
> intervene to make that safe; it is the default."

### 2d. Approve as a reviewer

In the Models UI, on version 5, add tags:

```
approval_status = approved
approved_by     = risk.reviewer
```

> "The approval is a Unity Catalog tag, so it is itself a governed object — versioned,
> permissioned, and captured in lineage. It isn't a Slack message or a ticket comment."

Re-run only the gate:

```bash
databricks bundle run model_training_job -t prod --only ApprovalGate
```

```
APPROVED
```

> "Re-running one task, not the whole pipeline. The reviewer's decision is the only thing
> that changed."

---

## Act 3 — Rollback (3 min)

**Point:** promotion and rollback are the same cheap operation in opposite directions.

```bash
databricks bundle run rollback_job -t dev
```

```
ROLLED_BACK:5
```

Show the alias moving v6 → v5 in the Models tab.

> "Seconds. No redeployment, no retraining, no code change. Every scoring job resolves
> `models:/…@champion` at run time, so re-pointing the alias is the rollback."

Then the deliberate design decision:

> "Note that rollback is **not** gated on approval. Requiring sign-off to *stop* serving a
> bad model would extend the incident. The gate controls what goes live; it must not
> obstruct taking something down. The rollback is still fully audited."

Target a specific version:

```bash
databricks bundle run rollback_job -t dev \
  --params target_version=4,reason="AUC decay observed in production"
```

---

## Act 4 — Audit, monitoring, and the decision layer (8 min)

### 4a. The audit trail

```sql
SELECT promoted_version, environment, approver, decision, evaluation_summary
FROM paypay_demo_catalog.payments_dev.promotion_audit_log
ORDER BY recorded_at DESC;
```

Real output:

| version | environment | approver | decision |
|---|---|---|---|
| 5 | dev | rollback-operator:dev | ROLLED_BACK |
| 6 | dev | service-principal:dev | APPROVED_AND_PROMOTED |
| 4 | dev | service-principal:dev | APPROVED_AND_PROMOTED |
| 3 | dev | service-principal:dev | APPROVED_AND_PROMOTED |

> "Two things worth noticing. First, both directions of every alias move are recorded —
> promotions and rollbacks. Second, look at which version is **missing**: v5 was blocked,
> so it has no promotion record. The audit trail proves the gate held."

Each row carries the metrics the approver actually saw:

```json
{"roc_auc": 0.871, "pr_auc": 0.738, "precision_score": 0.562,
 "recall_score": 0.837, "f1_score": 0.672}
```

And the immutable evidence files:

```sql
LIST '/Volumes/paypay_demo_catalog/payments_dev/audit_logs';
```

> "Unity Catalog system tables already track *what* happened. The Volume answers *on what
> basis* — the metrics, the training-data Delta version, the approver, at the moment of
> the decision. That's what an auditor asks for."

### 4b. Drift monitoring and gated retraining

```bash
databricks bundle run monitoring_job -t dev
```

```
Task SetupMonitor:  MONITOR_READY
Task DriftCheck:    STABLE
```

```sql
SELECT feature, round(psi, 4) AS psi, verdict
FROM paypay_demo_catalog.payments_dev.drift_check_results
ORDER BY psi DESC;
```

| feature | psi | verdict |
|---|---|---|
| age | 0.0663 | STABLE |
| credit_amount | 0.0559 | STABLE |
| monthly_instalment | 0.0284 | STABLE |

> "PSI is the metric credit-risk teams actually govern on, and it isn't built into
> Lakehouse Monitoring. We register it once as a Unity Catalog function, so the credit team
> and the fraud team compute drift with the identical audited formula. Our integration
> tests assert the SQL matches the Python implementation — the tested logic *is* the
> deployed logic."

**If you want to show a breach** (adds ~6 min), inject a shifted cohort:

```sql
INSERT INTO paypay_demo_catalog.payments_dev.inference_log
SELECT customer_id, duration*3, credit_amount*5, age+35, installment_commitment,
       monthly_instalment*4, credit_to_age_ratio*3, prediction, model_version,
       scored_at, ground_truth
FROM paypay_demo_catalog.payments_dev.inference_log LIMIT 150;
```

Re-run `monitoring_job`. Real result:

| feature | psi | verdict |
|---|---|---|
| age | 0.9432 | RETRAIN |
| monthly_instalment | 0.5922 | RETRAIN |
| duration | 0.4709 | RETRAIN |
| installment_commitment | 0.0139 | STABLE |

> "It caught the four features I shifted and left the one I didn't alone — the metric is
> discriminating, not just alarming."

Then the governance point:

> "Drift triggered *retraining*, and the retrained model entered as a **challenger**. In
> production it stops at the same approval gate. Automated drift response must never become
> an unreviewed path into production — otherwise it's a back door around everything we just
> showed."

Afterwards, clean up: `TRUNCATE TABLE paypay_demo_catalog.payments_dev.inference_log;` then re-run
`batch_inference_job`.

### 4c. The decision layer

```sql
SELECT risk_band, count(*) AS applicants,
       round(avg(prediction), 3) AS avg_pd,
       round(avg(offered_credit_limit), 0) AS avg_limit
FROM paypay_demo_catalog.payments_dev.credit_decisions
GROUP BY risk_band ORDER BY risk_band;
```

| risk_band | applicants | avg_pd | avg_limit |
|---|---|---|---|
| BAND_B_MEDIUM_RISK | 73 | 0.212 | 2096 |
| BAND_C_HIGH_RISK | 121 | 0.351 | 1846 |
| BAND_D_EXCESSIVE_RISK | 5 | 0.505 | 0 |

Open `decision_layer/policy.py`.

> "The model predicts a probability of default. That is *all* it does — it does not decide
> whether to grant credit. Eligibility rules, risk bands and limit caps are separate SQL
> functions in Unity Catalog."

> "That split matters because policy changes far more often than models. A regulator caps a
> limit, or risk tightens a band — that ships as a function update, with no retraining, no
> re-scoring, and no model risk review. And note the hard filters run *before* the
> probability bands, so a very low PD can never override a KYC failure."

```sql
SELECT rejection_reason, count(*) FROM paypay_demo_catalog.payments_dev.credit_decisions
GROUP BY rejection_reason;
```

> "Every rejection carries a deterministic reason code. When someone asks why an applicant
> was declined, the answer is a value in a column, not a model interpretation exercise."

---

## Act 5 — Testing (3 min)

```bash
pytest tests/unit -q         # 83 tests, ~45s, no cluster needed
pytest tests/integration -q  # 19 tests against a real workspace
```

> "Three tiers. Unit tests run offline on every commit — feature transforms, the PSI
> formula, the promotion gate, the policy rules. Integration tests verify the same logic
> against real Unity Catalog. And the staging integration test runs the entire pipeline on
> every pull request."

Worth saying plainly:

> "That third tier is not ceremony. Building this platform, the staging run caught an MLflow
> API rename, a schema mismatch between the drift baseline and the inference log, and an SDK
> argument that is valid on create but rejected on update. None of those were visible to
> unit tests or to bundle validation. The pipeline has to actually run."

---

## Closing

Return to the one sentence:

> "No model reaches production without an automated quality bar and a named human's
> approval, and any model can be removed in seconds. Everything else — the drift
> monitoring, the decision layer, the audit Volume — exists to make that claim
> defensible to an auditor."

### What this maps to in the design document

| Design section | Where it is in the repo |
|---|---|
| §3.3 Unity Catalog layout | `databricks.yml` variables, `platform_utils/naming.py` |
| §3.4 Config-driven template | `platform_utils/` as a shared layer; project code stays thin |
| §3.5 CI/CD golden path | `.github/workflows/*` |
| §3.6 Two-stage approval + rollback | `deployment/approval/`, `deployment/model_deployment/` |
| §3.7 Monitoring & retraining | `monitoring/`, `platform_utils/metrics.py` |
| §3.11 Testing strategy | `tests/unit/`, `tests/integration/` |
| §3.15 Decision layer | `decision_layer/` |

### Known gaps — say these before you are asked

- **Distributed HPO (§3.14)** is not built. Training uses fixed hyperparameters. The
  Optuna-with-Ray pattern is straightforward to add but was out of scope here.
- **Real-time serving and the online feature store (§3.8/§3.9)** are not built. This is a
  batch platform; the alias-based promotion design carries over to serving endpoints
  unchanged.
- **FinOps tagging and budgets (§3.12)** are not implemented.
- **Approval is a UC tag**, not a portal click. Wiring it to an internal developer portal
  is an integration exercise; the control itself is already enforced.
- Schema-per-environment is a **weaker** isolation posture than catalog-per-environment.
  It is used here because the demo workspace denies `CREATE CATALOG`. Because both are
  addressed through the same variables, migrating is a config change per target — no
  pipeline code moves.

### If the demo breaks

| Symptom | Cause | Fix |
|---|---|---|
| `Schema ... does not exist` | bootstrap not run | `scripts/bootstrap_uc.py`, or pass `--var schema_name=<owned schema>` |
| `No @champion alias` | scored before training | run `model_training_job` first |
| Gate does not block | `approval_required` baked at deploy time | re-**deploy** with the variable set; `--var` on `run` alone will not change it |
| Monitor task fails | inference log missing | run `batch_inference_job` first |
| Token refresh timeout | transient | re-run the command |
