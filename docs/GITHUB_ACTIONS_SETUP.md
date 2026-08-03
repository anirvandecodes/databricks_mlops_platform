# Configuring GitHub Actions

What to create in GitHub so the four CI/CD workflows run. Roughly 15 minutes.

## The four workflows

| Workflow | Fires on | Does |
|---|---|---|
| `...-run-tests.yml` | PR | unit tests, then integration tests |
| `...-bundle-ci.yml` | PR | validates all 3 targets, then runs the whole pipeline in staging |
| `...-bundle-cd-staging.yml` | merge to `main` | deploys + runs pipeline in staging |
| `...-bundle-cd-prod.yml` | push to `release` | deploys to prod, trains, then **stops at the approval gate** |

The two PR workflows are the Stage-1 code gate. Make them required status checks and the
gate has teeth.

---

## Step 1 — Create service principals (recommended) or get tokens

Workflows authenticate to Databricks as a **service principal**, not as a person. A human's
token ties production deploys to one employee's account and breaks when they rotate
credentials or leave.

Create one SP per environment, each with:
- workspace access
- `USE CATALOG` + `CREATE TABLE` / `CREATE FUNCTION` / `CREATE VOLUME` on its schema
- `CAN_MANAGE` on that environment's bundle root path

> **Note for this demo workspace:** the current identity cannot create tokens
> (`User does not have permission to use tokens`), and cannot create schemas. Both need a
> workspace admin. See [DEMO_SCRIPT.md](DEMO_SCRIPT.md) for the schema bootstrap.

### Option A — OAuth (M2M), preferred

No long-lived secret to rotate. Create an OAuth secret for the SP, then set
`DATABRICKS_CLIENT_ID` / `DATABRICKS_CLIENT_SECRET` instead of `DATABRICKS_TOKEN`. The
Databricks CLI picks these up automatically — no workflow edit needed beyond swapping the
two `env:` lines.

### Option B — Personal access token

Simpler, but long-lived. Generate a PAT per SP and use `DATABRICKS_TOKEN`.

---

## Step 2 — Add repository secrets

**Settings → Secrets and variables → Actions → Secrets → New repository secret**

Always set the two hosts:

| Secret | Value |
|---|---|
| `STAGING_WORKSPACE_HOST` | `https://<workspace>.cloud.databricks.com` |
| `PROD_WORKSPACE_HOST` | prod workspace URL (same host here — environments are separated by schema) |

Then **one** of the following credential pairs per environment. The workflows read both, and
the Databricks CLI prefers OAuth when the client id/secret are present, so you can start with
a PAT and move to OAuth later without editing any workflow.

| OAuth (preferred) | Personal access token |
|---|---|
| `STAGING_CLIENT_ID` / `STAGING_CLIENT_SECRET` | `STAGING_WORKSPACE_TOKEN` |
| `PROD_CLIENT_ID` / `PROD_CLIENT_SECRET` | `PROD_WORKSPACE_TOKEN` |

Leave the unused ones unset — an empty secret resolves to an empty string, which the CLI
ignores.

Or with the CLI, authenticated as the repo owner:

```bash
gh auth login   # must be the account that owns the repo
R=anirvandecodes/databricks_mlops_platform

gh secret set STAGING_WORKSPACE_HOST  --repo $R --body "https://dbc-aef35066-afa2.cloud.databricks.com"
gh secret set STAGING_WORKSPACE_TOKEN --repo $R   # prompts, not in shell history
gh secret set PROD_WORKSPACE_HOST     --repo $R --body "https://dbc-aef35066-afa2.cloud.databricks.com"
gh secret set PROD_WORKSPACE_TOKEN    --repo $R
```

Set the token secrets interactively (no `--body`) so they never land in your shell history.

---

## Step 3 — Add repository variables

**Same page → Variables tab.** These are non-sensitive, so they're variables not secrets.

| Variable | Value | Purpose |
|---|---|---|
| `MLOPS_TEST_CATALOG` | `workspace` | catalog integration tests write to |
| `MLOPS_TEST_SCHEMA` | `mlops_staging` | schema integration tests write to |

```bash
gh variable set MLOPS_TEST_CATALOG --repo $R --body "workspace"
gh variable set MLOPS_TEST_SCHEMA  --repo $R --body "mlops_staging"
```

Both have defaults in the workflow, so this step is optional.

---

## Step 4 — Create the `production` environment ★

This is where the governance story gets a second control.

**Settings → Environments → New environment → `production`**

Then configure:

1. **Required reviewers** — add whoever owns model risk. The prod CD workflow's
   `train_and_stage_candidate` job now pauses until one of them approves, *before* it runs.
2. **Deployment branches** — restrict to `release` only.
3. Optionally move `PROD_WORKSPACE_TOKEN` here as an environment secret, so prod credentials
   are unreachable from any workflow not targeting `production`.

You now have three independent gates:

| Gate | Enforced by | Blocks |
|---|---|---|
| Code | branch protection + PR checks | unreviewed or failing code |
| Deployment | GitHub environment reviewers | the prod job starting at all |
| Model | `ApprovalGate` task in the pipeline | an unapproved *version* serving traffic |

The third is the one that matters most, because it is per-model-version. The first two
approve code and a run; only the model gate approves the specific artifact.

---

## Step 5 — Protect the branches

**Settings → Branches → Add branch ruleset** for `main`:

- Require a pull request, 1+ approval
- Require status checks to pass — select:
  - `unit_tests`
  - `integration_tests`
  - `validate (dev)`, `validate (staging)`, `validate (prod)`
  - `staging_integration`
  - `lint` (workflow-file linting)
- Block force pushes

Status checks only appear in that list after they've run once, so open a throwaway PR first,
then come back and select them.

Add the same ruleset for `release`, since that branch deploys to production.

---

## Step 6 — Verify

```bash
git checkout -b ci-verify
git commit --allow-empty -m "Verify CI wiring"
git push -u origin ci-verify
gh pr create --fill
gh pr checks --watch
```

Expect: unit tests pass in ~2 min; bundle validation passes for all three targets; the
staging integration run takes ~10 min and exercises the full pipeline.

Then merge and confirm staging CD runs. When you push to `release`, the prod workflow should
**deploy successfully but report "Candidate awaiting approval"** — that is the gate working,
not a failure.

---

## Runner network access

GitHub-hosted runners reach Databricks over the public internet. If the workspace restricts
ingress by IP or sits behind PrivateLink, hosted runners cannot connect. Options:

- Self-hosted runners inside the VPC (usual answer for a locked-down workspace)
- Add GitHub's IP ranges to the workspace IP access list (broad; weakens the control)

Worth resolving early — it is the most common reason this setup stalls, and it is an
infrastructure decision rather than a CI one.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `default auth: cannot configure default credentials` | secret missing or misnamed | check exact names in Step 2 |
| `Schema ... does not exist` | UC bootstrap not run | admin creates the three schemas |
| `403` on deploy | SP lacks grants on schema or bundle path | grant per Step 1 |
| `integration_tests` skipped | PR from a fork | expected; forks can't read secrets |
| Prod CD "fails" at ApprovalGate | **working as designed** | tag the version approved, re-run `--only ApprovalGate` |
| Status check not selectable | never ran | open a PR once, then add it |

## Cleaning up

`.github/workflows/deploy-cicd.yml` is left over from the MLOps Stacks generator. It
references a `WORKFLOW_TOKEN` secret that nothing else needs, and has two shell bugs
(`$(PROJECT_NAME_ALPHA)` instead of `${...}`, and a backslash path separator). It is
`workflow_dispatch`-only so it never fires on its own — delete it unless you plan to
scaffold more projects from this repo.
