"""Feature-contract change check — the CI gate for shared feature tables.

Compares the contract on the base branch with the proposed contract and decides whether
the change may ship:

  1. Is the proposed contract valid?               (validate_contract)
  2. What kind of change is it?                    (none / metadata / additive / breaking)
  3. Does it break a rule?                         (in-place logic/dtype edit, key change,
                                                    removing an active feature, missing bump)
  4. Who must approve, and have they?              (producer always; affected consumers
                                                    when breaking — from the change request)

Exit code 0 = may merge/release, 1 = blocked. Runs locally and in GitHub Actions
(.github/workflows/feature_sharing_demo-contract-check.yml).

    python governance/check_change.py \
        --base team_a_producer/contracts/customer_features.yaml \
        --proposed team_a_producer/changes/v1.1.0.yaml \
        --change-request governance/change_requests/CR-001.yaml
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared"))

import feature_contract_utils as fcu  # noqa: E402

ICON = {"none": "·", "metadata": "~", "additive": "+", "breaking": "!"}


def run(base_path: str, proposed_path: str, cr_path: str | None) -> int:
    base = fcu.load_yaml(base_path)
    proposed = fcu.load_yaml(proposed_path)
    cr = fcu.load_yaml(cr_path) if cr_path else {}

    print(f"Feature contract check: {proposed['table']}")
    print(f"  base {base['version']}  ->  proposed {proposed['version']}\n")

    problems = fcu.validate_contract(proposed)
    if problems:
        print("INVALID CONTRACT")
        for p in problems:
            print(f"  x {p}")
        return 1

    result = fcu.classify_change(base, proposed)
    print(f"Change kind: {result['kind'].upper()}")
    for c in result["changes"]:
        print(f"  {ICON[c['kind']]} {c['feature']:<22} {c['kind']:<9} {c['message']}")
    if not result["changes"]:
        print("  (no changes)")

    blocked = False
    if result["violations"]:
        blocked = True
        print("\nBLOCKED — rule violations:")
        for v in result["violations"]:
            print(f"  x {v}")

    consumers = cr.get("affected_consumers", []) or []
    required = fcu.required_approvers(result, proposed, consumers)
    if required:
        print(f"\nRequired approvals: {', '.join(required)}"
              + ("  (breaking: producer + every affected consumer)" if result["kind"] == "breaking"
                 else "  (non-breaking: producer only)"))
        if not cr:
            blocked = True
            print("  x no change request supplied (--change-request)")
        else:
            missing = fcu.missing_approvals(required, cr)
            for team in required:
                print(f"  {'x missing ' if team in missing else 'v approved'}  {team}")
            if missing:
                blocked = True

    print("\nRESULT:", "BLOCKED" if blocked else "OK to merge and release")
    return 1 if blocked else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--base", required=True, help="contract on the base branch (currently released)")
    parser.add_argument("--proposed", required=True, help="contract in the PR")
    parser.add_argument("--change-request", help="change request YAML with affected_consumers + approvals")
    args = parser.parse_args()
    sys.exit(run(args.base, args.proposed, args.change_request))


if __name__ == "__main__":
    main()
