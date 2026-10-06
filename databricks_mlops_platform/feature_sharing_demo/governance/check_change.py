"""Feature-contract PR gate: will this change break anything downstream?

Compares the contract on the base branch (main) with the contract in the PR:

  1. Is the proposed contract valid?                 (validate_contract)
  2. What changed?                                   (added / deprecated / changed / removed / ...)
  3. Is the change allowed at all?                   (no key/dtype edits in place, semver bump,
                                                      changelog entry)
  4. Does anything read a changed or removed column?             (dependencies.py)
     Live model versions (UC lineage + their feature specs; alias or serving endpoint) and
     jobs, pipelines and dashboards (column lineage, latest run). If a source can't be read,
     the PR is blocked: an unknown answer is not a yes.

Exit code 0 = safe to merge, 1 = blocked. Code-owner review (CODEOWNERS) is the approval;
this gate is the only thing that decides whether downstream breaks.

    python governance/check_change.py \
        --base /tmp/base.yaml \
        --proposed team_a_producer/contracts/customer_features.yaml \
        --var catalog=workspace --var producer_schema=team_a_features_staging \
        --var raw_schema=feature_demo_raw_staging

Authenticates like the Databricks CLI (DATABRICKS_HOST/DATABRICKS_TOKEN in CI, or
DATABRICKS_CONFIG_PROFILE locally); lineage is read through a SQL warehouse
(DATABRICKS_WAREHOUSE_ID, or the first serverless one). --offline skips the lookup.
"""

import argparse
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared"))

import feature_contract_utils as fcu  # noqa: E402
from dependencies import Report, find_dependencies, log  # noqa: E402

ICON = {"added": "+", "deprecated": "~", "metadata": "~", "changed": "!", "removed": "!", "keys": "!"}


def run(base_path: str, proposed_path: str, variables: dict[str, str],
        dependencies_of: Callable[[str], Report | None] | None = find_dependencies) -> int:
    base = fcu.load_yaml(base_path, variables)
    proposed = fcu.load_yaml(proposed_path, variables)

    print(f"Feature contract check: {proposed['table']}")
    print(f"  main {base['version']}  ->  PR {proposed['version']}\n")
    log(f"Checking {proposed['table']}: main {base['version']} -> PR {proposed['version']}")

    log("Step 1/4  Is the proposed contract valid?")
    problems = fcu.validate_contract(proposed)
    log("          " + ("no: " + "; ".join(problems) if problems else "yes"))
    if problems:
        print("BLOCKED — invalid contract:")
        for p in problems:
            print(f"  x {p}")
        return 1

    log("Step 2/4  What changed compared with main?")
    changes = fcu.diff_contract(base, proposed)
    log("          " + (", ".join(f"{c['feature']} ({c['kind']})" for c in changes) or "nothing"))
    print("Changes:")
    for c in changes:
        print(f"  {ICON[c['kind']]} {c['feature']:<22} {c['kind']:<10} {c['message']}")
    if not changes:
        print("  (none)")

    blocked = False
    log("Step 3/4  Is the change allowed? (version bump, changelog, no dtype/key edits in place)")
    problems = fcu.change_problems(base, proposed, changes)
    log("          " + ("no: " + "; ".join(problems) if problems else "yes"))
    if problems:
        blocked = True
        print("\nBLOCKED — not allowed:")
        for p in problems:
            print(f"  x {p}")

    breaking = [c for c in changes if c["kind"] in fcu.BREAKING_KINDS]
    log("Step 4/4  Does anything downstream read a changed or removed column?"
        + (f"  (changed/removed: {', '.join(c['feature'] for c in breaking)})" if breaking
           else "  (no column is changed or removed)"))
    report = dependencies_of(base["table"]) if changes and dependencies_of else None
    if changes and dependencies_of and report is None:
        print(f"\n{base['table']} isn't published here yet, so nothing can read it — nothing to break.")
    elif report:
        keys = set(base["primary_keys"]) | {base["timestamp_key"]}
        print_dependencies(report, keys)
        deps = [asdict(d) for d in report.dependencies]
        breaks = fcu.downstream_breaks(breaking, [d for d in deps if d["blocking"]])
        warnings = fcu.downstream_breaks(breaking, [d for d in deps if not d["blocking"]])
        if report.errors:
            blocked = True
            print("\nBLOCKED — dependencies couldn't be fully checked, so the change can't be proven safe:")
            for e in report.errors:
                print(f"  x {e}")
        if breaks:
            blocked = True
            print("\nBLOCKED — this would break downstream:")
            for b in breaks:
                print(f"  x {_label(b['dependency'])} reads {b['feature']}: {b['message']}")
            for feature in sorted({b["feature"] for b in breaks if not b["feature"].startswith("<")}):
                print(f"\nHow to ship it without breaking anyone:\n"
                      f"  1. Add the new logic as a new column, {fcu.next_version_name(feature)}, and leave {feature} unchanged.\n"
                      f"  2. Each dependency above moves to {fcu.next_version_name(feature)} (its own PR).\n"
                      f"  3. Once nothing reads {feature}, it can be changed or removed.")
        elif not report.errors:
            print("\nNothing that depends on this table reads a changed or removed column — nothing breaks.")
        if warnings:
            print("\nWarnings (ad hoc reads, not blocking — tell these people):")
            for b in warnings:
                print(f"  ! {_label(b['dependency'])} read {b['feature']} ({b['dependency']['detail']})")
    elif breaking:
        print("\n(offline: dependencies not checked)")

    print("\nRESULT:", "BLOCKED" if blocked else "OK to merge")
    log(f"Result: {'BLOCKED' if blocked else 'OK to merge'}")
    return 1 if blocked else 0


def _label(dep: dict[str, Any]) -> str:
    return f"{dep['kind']} {dep['name']}" + (f" {dep['detail']}" if dep["kind"] == "model" else "")


def print_dependencies(report: Report, keys: set[str]) -> None:
    """The full dependency picture for the table, whatever this PR changes."""
    def cols(d: Any) -> str:
        return "every column (unknown)" if d.features is None else ", ".join(f for f in d.features if f not in keys)

    groups = [
        ("Live models", [d for d in report.dependencies if d.kind == "model"]),
        ("Jobs, pipelines and dashboards (latest run)", [d for d in report.dependencies
                                                        if d.kind != "model" and d.blocking]),
        ("Ad hoc reads (warning only)", [d for d in report.dependencies if not d.blocking]),
    ]
    print(f"\nDependencies of {report.table}:")
    for title, deps in groups:
        print(f"  {title}:")
        for d in deps:
            who = f"{d.name} {d.detail}" if d.kind == "model" else f"{d.kind} {d.name}"
            print(f"    {who}\n      reads {cols(d)}   [{d.source}" +
                  ("" if d.kind == "model" else f"; {d.detail}") + "]")
        if not deps:
            print("    none")
    if report.not_counted:
        print("  Not counted:")
        for n in report.not_counted:
            print(f"    {n}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--base", required=True, help="contract on the base branch (main)")
    parser.add_argument("--proposed", required=True, help="contract in the PR")
    parser.add_argument("--var", action="append", default=[], metavar="NAME=VALUE",
                        help="value for a ${NAME} placeholder in the contract (repeatable)")
    parser.add_argument("--offline", action="store_true", help="skip the live dependency lookup")
    args = parser.parse_args()
    variables = dict(v.split("=", 1) for v in args.var)
    sys.exit(run(args.base, args.proposed, variables, None if args.offline else find_dependencies))


if __name__ == "__main__":
    main()
