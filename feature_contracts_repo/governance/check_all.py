"""PR gate for the whole repo: every feature table under features/, whichever team owns it.

Any team can add a feature table (a new features/<table>/ folder) or change one, so every PR
checks every folder against the base branch:

  - a folder on both sides     -> check_change.run (the full gate for a change)
  - a folder only in the PR    -> a new table: valid, its SQL compiles, its name is free
  - a folder only on main      -> blocked: a published table is never deleted by a PR
                                  (deprecate its features and remove them instead)
  - two contracts, one table   -> blocked: each UC table has exactly one contract

Exit code 0 = every table is safe to merge, 1 = something is blocked.

    python governance/check_all.py --base-root /tmp/base \
        --var catalog=workspace --var producer_schema=team_a_features_staging \
        --var raw_schema=feature_demo_raw_staging

--base-root is a checkout of the base branch at this repo's root (the folder holding
features/). Takes --offline like check_change.py.
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_change  # noqa: E402
import feature_contract_utils as fcu  # noqa: E402
from dependencies import compile_sql, find_dependencies  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def contracts(features_root: Path) -> dict[str, Path]:
    """{folder name: its contract} for every features/<table>/ holding a *.yaml."""
    if not features_root.is_dir():
        return {}
    return {d.name: sorted(d.glob("*.yaml"))[0]
            for d in sorted(features_root.iterdir()) if d.is_dir() and any(d.glob("*.yaml"))}


def run_all(base_root: Path, proposed_root: Path, variables: dict[str, str],
            dependencies_of=find_dependencies, compile_with=compile_sql) -> int:
    base, proposed = contracts(base_root / "features"), contracts(proposed_root / "features")
    blocked = []

    for name in sorted(set(base) - set(proposed)):
        print(f"::group::features/{name} (deleted)")
        print(f"BLOCKED — features/{name} was deleted. A published feature table isn't removed by "
              "deleting its folder: deprecate its features, then remove them once nothing reads them.")
        print("::endgroup::")
        blocked.append(name)

    tables = {name: fcu.load_yaml(path, variables).get("table") for name, path in proposed.items()}
    for table, n in Counter(tables.values()).items():
        if n > 1:
            owners = ", ".join(f"features/{k}" for k, t in tables.items() if t == table)
            print(f"BLOCKED — {table} is declared by more than one contract ({owners})")
            blocked.append(table)

    for name, path in proposed.items():
        print(f"::group::features/{name}" + ("" if name in base else " (new)"))
        code = check_change.run(str(base[name]) if name in base else None, str(path), variables,
                                dependencies_of, compile_with)
        print("::endgroup::")
        if code:
            blocked.append(name)

    print("\nRESULT:", f"BLOCKED ({', '.join(blocked)})" if blocked else
          f"OK to merge ({len(proposed)} feature table(s) checked)")
    return 1 if blocked else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--base-root", required=True, help="checkout of the base branch at the repo root")
    parser.add_argument("--var", action="append", default=[], metavar="NAME=VALUE",
                        help="value for a ${NAME} placeholder in the contracts (repeatable)")
    parser.add_argument("--offline", action="store_true", help="skip the SQL compile and dependency lookups")
    args = parser.parse_args()
    variables = dict(v.split("=", 1) for v in args.var)
    sys.exit(run_all(Path(args.base_root), ROOT, variables,
                     *((None, None) if args.offline else (find_dependencies, compile_sql))))


if __name__ == "__main__":
    main()
