"""Feature-contract PR gate: is this change to a shared feature table safe to ship?

Compares one feature table on the base branch (main) with the PR — its self-contained
folder features/<table>/ (the <table>.yaml contract and its _base.sql + <feature>.sql):

  1. Are the contract and SQL valid, and do they match one to one?
  2. What changed?                 added / deprecated / removed / changed / metadata
  3. Is it allowed?                released features are immutable (no SQL, dtype, key or
                                   query-skeleton edits in place); only deprecated features can
                                   be removed; semver bump; changelog entry
  4. Does the SQL compile?         EXPLAIN in a SQL warehouse, for added or changed features
  5. What depends on it?           for changes that affect existing readers (dependencies.py):
                                   live models (UC lineage + feature specs; alias or serving
                                   endpoint) and jobs, pipelines and dashboards (column lineage,
                                   latest run). A removal is blocked while anything still reads
                                   the column; if a source can't be read, the PR is blocked.

Exit code 0 = safe to merge, 1 = blocked. Approval is a code-owner review (CODEOWNERS).

    python governance/check_change.py \
        --base /tmp/base/features/customer_features/customer_features.yaml \
        --proposed features/customer_features/customer_features.yaml \
        --var catalog=workspace --var producer_schema=team_a_features_staging \
        --var raw_schema=feature_demo_raw_staging

For a table that isn't on main yet, leave out --base: the contract must be valid, every
feature's SQL must compile and the table name must be free.

The SQL is found in the contract's own folder (features/<table>/), so --base points into
a checkout of the base branch. Authenticates like the Databricks CLI (DATABRICKS_HOST and
DATABRICKS_TOKEN or DATABRICKS_CLIENT_ID/SECRET in CI, DATABRICKS_CONFIG_PROFILE locally);
SQL runs in DATABRICKS_WAREHOUSE_ID or the first serverless warehouse. --offline skips steps
4 and 5.
"""

import argparse
import datetime
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared"))

import feature_contract_utils as fcu  # noqa: E402
from dependencies import Report, compile_sql, find_dependencies, log  # noqa: E402

ICON = {"added": "+", "deprecated": "~", "metadata": "~", "changed": "!", "removed": "-", "keys": "!",
        "revoked": "-"}


def run(base_path: str | None, proposed_path: str, variables: dict[str, str],
        dependencies_of: Callable[[str], Report | None] | None = find_dependencies,
        compile_with: Callable[[str, list[str]], str | None] | None = compile_sql) -> int:
    if base_path is None:
        return run_new(proposed_path, variables, dependencies_of, compile_with)
    base, proposed = fcu.load_yaml(base_path, variables), fcu.load_yaml(proposed_path, variables)
    base_sql = fcu.load_feature_sql(base_path, variables)
    proposed_sql = fcu.load_feature_sql(proposed_path, variables)

    print(f"Feature contract check: {proposed['table']}")
    print(f"  main {base['version']}  ->  PR {proposed['version']}\n")
    log(f"Checking {proposed['table']}: main {base['version']} -> PR {proposed['version']}")

    log("Step 1/5  Are the contract and its SQL valid, one SQL file per feature?")
    problems = fcu.validate_contract(proposed) or fcu.validate_sql(proposed, proposed_sql)
    log("          " + ("no: " + "; ".join(problems) if problems else "yes"))
    if problems:
        print("BLOCKED — invalid contract or SQL:")
        for p in problems:
            print(f"  x {p}")
        print("\nRESULT: BLOCKED")
        return 1

    log("Step 2/5  What changed compared with main?")
    changes = fcu.diff_contract(base, proposed, base_sql, proposed_sql)
    log("          " + (", ".join(f"{c['feature']} ({c['kind']})" for c in changes) or "nothing"))
    print("Changes:")
    for c in changes:
        print(f"  {ICON[c['kind']]} {c['feature']:<22} {c['kind']:<10} {c['message']}")
    if not changes:
        print("  (none)")

    blocked = False
    log("Step 3/5  Is it allowed? (immutable features, removal only after deprecation, semver, changelog)")
    problems = fcu.change_problems(base, proposed, changes)
    log("          " + ("no: " + "; ".join(problems) if problems else "yes"))
    if problems:
        blocked = True
        print("\nBLOCKED — not allowed:")
        for p in problems:
            print(f"  x {p}")
    warnings = fcu.change_warnings(base, changes, datetime.date.today().isoformat())

    touched = [c["feature"] for c in changes if c["kind"] in (fcu.ADDED, fcu.CHANGED)]
    if touched and compile_with:
        log(f"Step 4/5  Does the feature SQL compile? (EXPLAIN; added/changed: {', '.join(touched)})")
        error = compile_with(fcu.features_sql(proposed, proposed_sql), proposed["sources"])
        if error:
            blocked = True
            print(f"\nBLOCKED — the feature SQL doesn't compile:\n  x {error}")
    else:
        log("Step 4/5  Does the feature SQL compile? — skipped (no feature SQL added or changed)")

    breaking = [c for c in changes if c["kind"] in fcu.BREAKING_KINDS]
    report, published = None, True
    if breaking and dependencies_of:
        log("Step 5/5  What depends on what this changes? (" + ", ".join(
            f"{c['feature']} {c['kind']}" for c in breaking) + ")")
        report = dependencies_of(base["table"])
        published = report is not None
    else:
        log("Step 5/5  What depends on it? — skipped (no existing feature changes)")

    if not published:
        print(f"\n{base['table']} isn't published here yet, so nothing can read it — nothing to break.")
    elif report:
        keys = set(base["primary_keys"]) | {base["timestamp_key"]}
        print_dependencies(report, keys)
        deps = [asdict(d) for d in report.dependencies]
        breaks = fcu.downstream_breaks(breaking, [d for d in deps if d["blocking"]])
        adhoc = fcu.downstream_breaks(breaking, [d for d in deps if not d["blocking"]])
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
                new = fcu.next_version_name(feature)
                print(f"\nHow to ship it without breaking anyone:\n"
                      f"  1. Add the new logic as a new feature, {new} (contract entry + {new}.sql).\n"
                      f"  2. Deprecate {feature} (sunset_date, replaced_by: {new}).\n"
                      f"  3. Each dependency above moves to {new} in its own PR.\n"
                      f"  4. Once nothing reads {feature}, remove it (major version).")
        elif not report.errors:
            print("\nNothing that depends on this table reads a changed or removed column.")
        warnings += [f"{_label(b['dependency'])} read {b['feature']} ({b['dependency']['detail']})" for b in adhoc]
    elif breaking:
        print("\n(offline: dependencies not checked)")
    elif changes:
        print("\nNo existing feature changes, so nothing that reads this table can break.")

    if warnings:
        print("\nWarnings (not blocking):")
        for w in warnings:
            print(f"  ! {w}")

    print("\nRESULT:", "BLOCKED" if blocked else "OK to merge")
    log(f"Result: {'BLOCKED' if blocked else 'OK to merge'}")
    return 1 if blocked else 0


def run_new(proposed_path: str, variables: dict[str, str],
            dependencies_of: Callable[[str], Report | None] | None = find_dependencies,
            compile_with: Callable[[str, list[str]], str | None] | None = compile_sql) -> int:
    """A feature table that isn't on main yet: valid, every feature's SQL compiles, and the
    table name isn't already taken in Unity Catalog."""
    proposed = fcu.load_yaml(proposed_path, variables)
    proposed_sql = fcu.load_feature_sql(proposed_path, variables)
    print(f"Feature contract check: {proposed['table']} (new table, {proposed['version']})\n")
    log(f"Checking new table {proposed['table']} @ {proposed['version']}")

    log("Step 1/3  Are the contract and its SQL valid, one SQL file per feature?")
    problems = fcu.validate_contract(proposed) or fcu.validate_sql(proposed, proposed_sql)
    if not problems and not any(str(e.get("version")) == str(proposed["version"])
                                for e in proposed.get("changelog", [])):
        problems = [f"changelog has no entry for {proposed['version']}"]
    if problems:
        print("BLOCKED — invalid contract or SQL:")
        for p in problems:
            print(f"  x {p}")
        print("\nRESULT: BLOCKED")
        return 1
    print("Features:")
    for f in proposed["features"]:
        print(f"  + {f['name']:<22} {f['dtype']}")

    blocked = False
    if compile_with:
        log("Step 2/3  Does the feature SQL compile? (EXPLAIN; every feature)")
        error = compile_with(fcu.features_sql(proposed, proposed_sql), proposed["sources"])
        if error:
            blocked = True
            print(f"\nBLOCKED — the feature SQL doesn't compile:\n  x {error}")
    if dependencies_of:
        log("Step 3/3  Is the table name free? (a new contract can't take over a published table)")
        if dependencies_of(proposed["table"]) is not None:
            blocked = True
            print(f"\nBLOCKED — {proposed['table']} already exists in Unity Catalog but not on main. "
                  "Pick another table name.")
    else:
        print("\n(offline: SQL and table name not checked)")

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
    parser.add_argument("--base", help="contract in a checkout of the base branch (main); omit for a new table")
    parser.add_argument("--proposed", required=True, help="contract in the PR")
    parser.add_argument("--var", action="append", default=[], metavar="NAME=VALUE",
                        help="value for a ${NAME} placeholder in the contract (repeatable)")
    parser.add_argument("--offline", action="store_true", help="skip the SQL compile and dependency lookups")
    args = parser.parse_args()
    variables = dict(v.split("=", 1) for v in args.var)
    sys.exit(run(args.base, args.proposed, variables, *((None, None) if args.offline
                                                          else (find_dependencies, compile_sql))))


if __name__ == "__main__":
    main()
