#!/usr/bin/env bash
# Driver for the feature-sharing demo. Each command is one step a team would normally do
# through Git (edit file -> PR -> CI check -> review -> merge -> deploy); here they run
# locally so the journey can be shown end to end without a GitHub repo.
#
#   ./demo.sh reset                      back to the starting state (contract 1.0.0, Team B on v1 config)
#   ./demo.sh deploy                     databricks bundle deploy -t dev (the whole platform bundle)
#   ./demo.sh run <job> [flags]          databricks bundle run <job> -t dev [flags]
#   ./demo.sh check <version> [CR-id]    CI gate: released contract vs team_a_producer/changes/<version>.yaml
#   ./demo.sh approve <CR-id> <team>     record a team's approval on a change request
#   ./demo.sh release <version> <CR-id>  gate passes -> "merge" the contract -> deploy -> run the producer pipeline
#   ./demo.sh migrate                    Team B pins the v2 features -> deploy -> retrain
#
# Env: DATABRICKS_CONFIG_PROFILE (default dbc-aef35066-afa2; ignored when DATABRICKS_HOST is set, e.g. in CI), PYTHON (default python3), TARGET (default dev)
set -euo pipefail
cd "$(dirname "$0")"

# In CI the CLI authenticates from DATABRICKS_HOST/DATABRICKS_TOKEN; forcing a profile there
# would fail because the runner has no ~/.databrickscfg.
if [[ -z "${DATABRICKS_HOST:-}" ]]; then
  export DATABRICKS_CONFIG_PROFILE="${DATABRICKS_CONFIG_PROFILE:-dbc-aef35066-afa2}"
fi
PYTHON="${PYTHON:-python3}"
TARGET="${TARGET:-dev}"
CONTRACT=team_a_producer/contracts/customer_features.yaml
CONSUMER_CFG=team_b_consumer/feature_config.yaml

cr_file() { echo "governance/change_requests/$1.yaml"; }

# The demo is part of the platform bundle, whose databricks.yml is one level up.
bundle() { (cd .. && databricks bundle "$@"); }

reset_approvals() {
  "$PYTHON" - "$1" <<'EOF'
import sys
p = sys.argv[1]
text = open(p).read()
head = text[: text.rindex("\napprovals:")]
open(p, "w").write(head + "\napprovals: []\n")
EOF
}

case "${1:-}" in
  reset)
    cp team_a_producer/changes/v1.0.0.yaml "$CONTRACT"
    cp team_b_consumer/changes/feature_config.v1.yaml "$CONSUMER_CFG"
    for f in governance/change_requests/CR-*.yaml; do reset_approvals "$f"; done
    echo "Reset: contract 1.0.0, Team B on v1 feature config, approvals cleared."
    echo "To also reset the workspace: ./demo.sh run feature_demo_setup  (recreates schemas)"
    ;;
  deploy)
    bundle deploy -t "$TARGET"
    ;;
  run)
    job="${2:?job name}"; shift 2
    bundle run "$job" -t "$TARGET" "$@"
    ;;
  check)
    version="${2:?version, e.g. v1.1.0}"
    args=(--base "$CONTRACT" --proposed "team_a_producer/changes/$version.yaml")
    [[ -n "${3:-}" ]] && args+=(--change-request "$(cr_file "$3")")
    "$PYTHON" governance/check_change.py "${args[@]}"
    ;;
  approve)
    cr="$(cr_file "${2:?CR id}")"; team="${3:?team}"
    "$PYTHON" - "$cr" "$team" <<'EOF'
import datetime, sys
p, team = sys.argv[1], sys.argv[2]
text = open(p).read().rstrip("\n")
if text.endswith("approvals: []"):
    text = text[: -len(" []")]
text += (f"\n  - team: {team}\n    approver: \"@{team.replace('_', '-')}-lead\"\n"
         f"    decision: approved\n    date: \"{datetime.date.today()}\"\n")
open(p, "w").write(text)
print(f"{team} approved {p}")
EOF
    ;;
  release)
    version="${2:?version}"; cr="${3:?CR id}"
    "$PYTHON" governance/check_change.py --base "$CONTRACT" \
      --proposed "team_a_producer/changes/$version.yaml" --change-request "$(cr_file "$cr")"
    cp "team_a_producer/changes/$version.yaml" "$CONTRACT"
    echo "Merged contract $version (main now = $version). Deploying and publishing..."
    bundle deploy -t "$TARGET"
    bundle run team_a_feature_pipeline -t "$TARGET"
    ;;
  migrate)
    cp team_b_consumer/changes/feature_config.v2.yaml "$CONSUMER_CFG"
    echo "Team B now pins txn_count_30d_v2 + avg_txn_amount_90d. Deploying and retraining..."
    bundle deploy -t "$TARGET"
    bundle run team_b_training -t "$TARGET"
    ;;
  *)
    sed -n '2,15p' "$0"; exit 1
    ;;
esac
