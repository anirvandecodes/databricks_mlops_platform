"""PR gate for the whole repo: every feature table under features/, whichever team owns it.

Any team can add a feature table (a new features/<table>/ folder) or change one, so every PR
checks every folder against the base branch:

  - a folder on both sides     -> check_change.run (the full gate for a change)
  - a folder only in the PR    -> a new table: valid, its SQL compiles, its name is free
  - a folder only on main      -> blocked: a published table is never deleted by a PR
                                  (deprecate its features and remove them instead)
  - two contracts, one table   -> blocked: each UC table has exactly one contract, in every
                                  environment (dev, staging and prod)
  - two contracts, one id      -> blocked: every ODCS contract id is unique (a copied folder
                                  needs a new one)

Exit code 0 = every table is safe to merge, 1 = something is blocked.

    python governance/check_all.py --base-root /tmp/base --env staging

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


def run_all(base_root: Path, proposed_root: Path, env: str,
            dependencies_of=find_dependencies, compile_with=compile_sql,
            legacy_vars: dict[str, str] | None = None) -> int:
    base, proposed = contracts(base_root / "features"), contracts(proposed_root / "features")
    blocked = []

    for name in sorted(set(base) - set(proposed)):
        print(f"::group::features/{name} (deleted)")
        print(f"BLOCKED — features/{name} was deleted. A published feature table isn't removed by "
              "deleting its folder: deprecate its features, then remove them once nothing reads them.")
        print("::endgroup::")
        blocked.append(name)

    raw = {}
    for name, path in proposed.items():
        try:
            raw[name] = fcu.load_yaml(path)
        except ValueError:
            pass  # not valid YAML: check_change reports it
    for e in fcu.ENVIRONMENTS:
        tables = {}
        for name, contract in raw.items():
            try:
                tables[name] = fcu.resolve(contract, e)["table"]
            except ValueError:
                pass  # check_change reports the invalid contract
        for table, n in Counter(tables.values()).items():
            if n > 1:
                owners = ", ".join(f"features/{k}" for k, t in tables.items() if t == table)
                print(f"BLOCKED — {table} ({e}) is declared by more than one contract ({owners})")
                blocked.append(table)

    for contract_id, n in Counter(c.get("id") for c in raw.values() if c.get("id")).items():
        if n > 1:
            owners = ", ".join(f"features/{k}" for k, c in raw.items() if c.get("id") == contract_id)
            print(f"BLOCKED — contract id {contract_id} is used by more than one contract ({owners}); "
                  "give each its own UUID")
            blocked.append(contract_id)

    for name, path in proposed.items():
        print(f"::group::features/{name}" + ("" if name in base else " (new)"))
        code = check_change.run(str(base[name]) if name in base else None, str(path), env,
                                dependencies_of, compile_with, legacy_vars)
        print("::endgroup::")
        if code:
            blocked.append(name)

    print("\nRESULT:", f"BLOCKED ({', '.join(blocked)})" if blocked else
          f"OK to merge ({len(proposed)} feature table(s) checked)")
    return 1 if blocked else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--base-root", required=True, help="checkout of the base branch at the repo root")
    parser.add_argument("--env", default="staging", choices=fcu.ENVIRONMENTS,
                        help="environment to check the tables in (where each lives: its server for <env>)")
    parser.add_argument("--legacy-var", action="append", default=[], metavar="NAME=VALUE",
                        help="placeholder value for base contracts from before environments: (repeatable)")
    parser.add_argument("--offline", action="store_true", help="skip the SQL compile and dependency lookups")
    args = parser.parse_args()
    legacy = dict(v.split("=", 1) for v in args.legacy_var)
    sys.exit(run_all(Path(args.base_root), ROOT, args.env,
                     *((None, None) if args.offline else (find_dependencies, compile_sql)), legacy_vars=legacy))


if __name__ == "__main__":
    main()
