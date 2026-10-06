"""Feature-contract PR gate: will this change break a model downstream?

Compares the contract on the base branch (main) with the contract in the PR:

  1. Is the proposed contract valid?                 (validate_contract)
  2. What changed?                                   (added / deprecated / changed / removed / ...)
  3. Is the change allowed at all?                   (no key/dtype edits in place, semver bump,
                                                      changelog entry)
  4. Does a live model read a changed or removed column?
     Asks the Unity Catalog model registry: every model version holding an alias
     (@champion, @challenger, ...) whose feature_dependencies tag lists the column.

Exit code 0 = safe to merge, 1 = blocked. Code-owner review (CODEOWNERS) is the approval;
this gate is the only thing that decides whether downstream breaks.

    python governance/check_change.py \
        --base /tmp/base.yaml \
        --proposed team_a_producer/contracts/customer_features.yaml \
        --var catalog=workspace --var producer_schema=team_a_features_staging \
        --var raw_schema=feature_demo_raw_staging

Authenticates like the Databricks CLI (DATABRICKS_HOST/DATABRICKS_TOKEN in CI, or
DATABRICKS_CONFIG_PROFILE locally). --offline skips the registry lookup.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared"))

import feature_contract_utils as fcu  # noqa: E402

ICON = {"added": "+", "deprecated": "~", "metadata": "~", "changed": "!", "removed": "!", "keys": "!"}


def live_consumers(table: str) -> list[dict[str, Any]] | None:
    """Aliased model versions in the table's catalog that read `table`, from their
    feature_dependencies tag (written by team_b_consumer/notebooks/02_train_with_lookups).
    None if the table isn't published in this workspace/environment yet."""
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.errors import NotFound

    w = WorkspaceClient()
    if not w.tables.exists(table).table_exists:
        return None
    catalog = table.split(".")[0]
    consumers = []
    for schema in w.schemas.list(catalog_name=catalog):
        if schema.name == "information_schema":
            continue
        for m in w.registered_models.list(catalog_name=catalog, schema_name=schema.name):
            # The MLflow UC endpoints return aliases and tags, which the UC model API does not.
            try:
                rm = w.api_client.do("GET", "/api/2.0/mlflow/unity-catalog/registered-models/get",
                                     query={"name": m.full_name})["registered_model"]
            except NotFound:
                continue
            team = {t["key"]: t["value"] for t in rm.get("tags", [])}.get("consumer_team", "unknown")
            for alias in rm.get("aliases", []):
                mv = w.api_client.do("GET", "/api/2.0/mlflow/unity-catalog/model-versions/get",
                                     query={"name": m.full_name, "version": alias["version"]})["model_version"]
                tags = {t["key"]: t["value"] for t in mv.get("tags", [])}
                features = json.loads(tags.get("feature_dependencies", "{}")).get(table)
                if features:
                    consumers.append({"team": team, "model": m.full_name, "version": int(alias["version"]),
                                      "alias": alias["alias"], "features": features})
    return consumers


def run(base_path: str, proposed_path: str, variables: dict[str, str],
        consumers_of: Callable[[str], list[dict[str, Any]] | None] | None = live_consumers) -> int:
    base = fcu.load_yaml(base_path, variables)
    proposed = fcu.load_yaml(proposed_path, variables)

    print(f"Feature contract check: {proposed['table']}")
    print(f"  main {base['version']}  ->  PR {proposed['version']}\n")

    problems = fcu.validate_contract(proposed)
    if problems:
        print("BLOCKED — invalid contract:")
        for p in problems:
            print(f"  x {p}")
        return 1

    changes = fcu.diff_contract(base, proposed)
    print("Changes:")
    for c in changes:
        print(f"  {ICON[c['kind']]} {c['feature']:<22} {c['kind']:<10} {c['message']}")
    if not changes:
        print("  (none)")

    blocked = False
    problems = fcu.change_problems(base, proposed, changes)
    if problems:
        blocked = True
        print("\nBLOCKED — not allowed:")
        for p in problems:
            print(f"  x {p}")

    breaking = [c for c in changes if c["kind"] in fcu.BREAKING_KINDS]
    consumers = consumers_of(base["table"]) if changes and consumers_of else []
    if consumers is None:
        print(f"\n{base['table']} isn't published here yet, so no model can read it — nothing to break.")
    elif changes and consumers_of:
        print(f"\nLive consumers of {base['table']} (aliased model versions):")
        for c in consumers:
            print(f"  {c['team']:<8} {c['model']} v{c['version']} @{c['alias']}  reads {c['features']}")
        if not consumers:
            print("  none")
        breaks = fcu.downstream_breaks(breaking, consumers)
        if not breaks:
            print("\nNo column a live model reads is changed or removed — nothing downstream breaks.")
        else:
            blocked = True
            print("\nBLOCKED — this would break downstream:")
            for b in breaks:
                k = b["consumer"]
                print(f"  x {k['model']} v{k['version']} @{k['alias']} ({k['team']}) reads "
                      f"{b['feature']}: {b['message']}")
            for feature in sorted({b["feature"] for b in breaks if not b["feature"].startswith("<")}):
                print(f"\nHow to ship it without breaking anyone:\n"
                      f"  1. Add the new logic as a new column, {fcu.next_version_name(feature)}, and leave {feature} unchanged.\n"
                      f"  2. The consumer moves to {fcu.next_version_name(feature)} in its own PR.\n"
                      f"  3. Once no live model reads {feature}, it can be changed or removed.")
    elif breaking:
        print("\n(offline: live consumers not checked)")

    print("\nRESULT:", "BLOCKED" if blocked else "OK to merge")
    return 1 if blocked else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--base", required=True, help="contract on the base branch (main)")
    parser.add_argument("--proposed", required=True, help="contract in the PR")
    parser.add_argument("--var", action="append", default=[], metavar="NAME=VALUE",
                        help="value for a ${NAME} placeholder in the contract (repeatable)")
    parser.add_argument("--offline", action="store_true", help="skip the live model-registry lookup")
    args = parser.parse_args()
    variables = dict(v.split("=", 1) for v in args.var)
    sys.exit(run(args.base, args.proposed, variables, None if args.offline else live_consumers))


if __name__ == "__main__":
    main()
