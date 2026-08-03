# Configuring GitHub Actions

What to create in GitHub so the four CI/CD workflows run. Roughly 15 minutes.

## The four workflows

| Workflow | Fires on | Does |
|---|---|---|
| `...-run-tests.yml` | PR | unit tests, then integration tests |
| `...-bundle-ci.yml` | PR | validates all 3 targets, then runs the whole pipeline in staging |
| `...-bundle-cd-staging.yml` | merge to `main` | deploys + runs pipeline in staging |
| `...-bundle-cd-prod.yml` | push to `release/**` | deploys to prod, trains, then **stops at the approval gate** |

The two PR workflows are the Stage-1 code gate. Make them required status checks and the
gate has teeth.

---

## Step 1 — Get an access token

For a demo, a personal access token for your own identity is fine.

For anything durable, use a **service principal** instead. A human's token ties production
deploys to one person's account and breaks when they rotate credentials or leave. The SP
needs:

- workspace access
- `USE CATALOG` plus `CREATE TABLE` / `CREATE FUNCTION` / `CREATE VOLUME` on its schema
- `CAN_MANAGE` on the bundle root path

Generate one under **User Settings → Developer → Access tokens**, or with
`databricks tokens create --comment "github actions"`.

If you later want to avoid a long-lived token entirely, Databricks supports OAuth (M2M):
create an OAuth secret for the SP and set `DATABRICKS_CLIENT_ID` / `DATABRICKS_CLIENT_SECRET`
in place of `DATABRICKS_TOKEN`. The CLI picks those up automatically — swapping the two
`env:` lines is the only workflow change.

---

## Step 2 — Add repository secrets

**Settings → Secrets and variables → Actions → Secrets → New repository secret**

Two secrets, used by all four workflows:

| Secret | Value |
|---|---|
| `DATABRICKS_HOST` | `https://fevm-paypay-demo.cloud.databricks.com` |
| `DATABRICKS_TOKEN` | access token for the identity CI runs as |

One pair covers every environment because staging and prod live in the **same workspace**,
separated by Unity Catalog schema. The Databricks CLI reads both variables automatically, so
no workflow changes are needed.

### The trade-off, stated plainly

A single repository secret means the staging pipeline holds credentials that can also write
to prod. The approval gate still prevents an unapproved *model* from serving traffic — it is
enforced inside the pipeline, not by the credential — but credential-level separation is
weaker than it could be.

Two ways to tighten it when you want to:

1. **Environment secrets.** Define `DATABRICKS_HOST` / `DATABRICKS_TOKEN` under
   Settings → Environments → `production` instead of as repository secrets. Workflows not
   targeting that environment cannot read them. Requires separate workspaces (or separate
   service principals) to be meaningful.
2. **Per-environment service principals.** One SP per environment, each granted only on its
   own schema, so a staging credential physically cannot write to `payments_prod`.

Either is a straightforward change later. Starting with one pair is a reasonable choice for a
demo and for a single-workspace deployment.

### Setting them

Via the UI, or with the CLI as the repo owner:

```bash
gh auth login   # must be the account that owns the repo
R=anirvandecodes/databricks_mlops_platform

gh secret set DATABRICKS_HOST  --repo $R --body "https://fevm-paypay-demo.cloud.databricks.com"
gh secret set DATABRICKS_TOKEN --repo $R   # prompts; keeps it out of shell history
```

Set the token with no `--body` so it is prompted for and never lands in shell history.

---

## Step 3 — Add repository variables

**Same page → Variables tab.** These are non-sensitive, so they're variables not secrets.

| Variable | Value | Purpose |
|---|---|---|
| `MLOPS_TEST_CATALOG` | `paypay_demo_catalog` | catalog integration tests write to |
| `MLOPS_TEST_SCHEMA` | `payments_staging` | schema integration tests write to |

```bash
gh variable set MLOPS_TEST_CATALOG --repo $R --body "paypay_demo_catalog"
gh variable set MLOPS_TEST_SCHEMA  --repo $R --body "payments_staging"
```

Both have defaults in the workflow, so this step is optional.

---

## Step 4 — Create the `production` environment ★

This is where the governance story gets a second control.

**Settings → Environments → New environment → `production`**

Then configure:

1. **Required reviewers** — add whoever owns model risk. The prod CD workflow's
   `promote` job pauses until one of them approves — *after* training and validation, so
   the reviewer decides with the candidate's metrics in front of them.
2. **Deployment branches** — restrict to the `release/*` pattern only.
3. Optionally define `DATABRICKS_TOKEN` here as an environment secret, overriding the
   repository one, so prod credentials are unreachable from any workflow not targeting
   `production`. Only meaningful once prod uses a distinct workspace or service principal.

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

Add the same ruleset for `release/*`, since those branches deploy to production.

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

Then merge and confirm staging CD runs. When you push a `release/<name>` branch, the prod
workflow should deploy, train, stage the candidate as `@challenger`, and then **pause on the
`promote` job awaiting your approval** — that pause is the gate working, not a failure.
Nothing reaches `@champion` until someone approves.

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
