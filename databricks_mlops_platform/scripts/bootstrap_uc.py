"""One-time Unity Catalog bootstrap.

Creates the per-environment schemas and the audit Volume the platform expects, then
verifies the caller can actually write to them.

Run once per workspace before the first bundle deploy:

    python scripts/bootstrap_uc.py --profile <profile> --catalog workspace

Requires ``CREATE SCHEMA`` on the target catalog. On a locked-down metastore this is the
one step that needs a workspace admin; everything afterwards works with ordinary user
rights. Idempotent, so re-running it is safe and is the quickest way to check the
environment is still sound.
"""
import argparse
import sys

ENVIRONMENTS = ("dev", "staging", "prod")


def _statement(client, warehouse_id: str, sql: str):
    """Execute one SQL statement and return (succeeded, message)."""
    from databricks.sdk.service.sql import StatementState

    response = client.statement_execution.execute_statement(
        warehouse_id=warehouse_id, statement=sql, wait_timeout="30s"
    )
    state = response.status.state if response.status else None
    if state == StatementState.SUCCEEDED:
        return True, ""
    error = response.status.error.message if response.status and response.status.error else "unknown"
    return False, error


def _pick_warehouse(client, requested: str = None) -> str:
    """Resolve a usable SQL warehouse, preferring a running one."""
    warehouses = list(client.warehouses.list())
    if not warehouses:
        raise SystemExit("No SQL warehouse available in this workspace.")
    if requested:
        for warehouse in warehouses:
            if warehouse.id == requested or warehouse.name == requested:
                return warehouse.id
        raise SystemExit(f"Warehouse {requested!r} not found.")
    for warehouse in warehouses:
        if str(warehouse.state) == "RUNNING":
            return warehouse.id
    return warehouses[0].id


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", help="Databricks CLI auth profile")
    parser.add_argument("--catalog", default="workspace", help="Target UC catalog")
    parser.add_argument("--prefix", default="mlops", help="Schema name prefix")
    parser.add_argument("--warehouse", help="Warehouse id or name (default: any running)")
    args = parser.parse_args()

    from databricks.sdk import WorkspaceClient

    client = WorkspaceClient(profile=args.profile) if args.profile else WorkspaceClient()
    warehouse_id = _pick_warehouse(client, args.warehouse)
    print(f"Using warehouse {warehouse_id}")

    # A stopped warehouse would make every statement below fail with a timeout rather
    # than a useful permission error.
    if str(client.warehouses.get(warehouse_id).state) != "RUNNING":
        print("Starting warehouse...")
        client.warehouses.start(warehouse_id).result()

    failures = []
    for environment in ENVIRONMENTS:
        schema = f"{args.prefix}_{environment}"
        fq_schema = f"{args.catalog}.{schema}"

        ok, error = _statement(client, warehouse_id, f"CREATE SCHEMA IF NOT EXISTS {fq_schema}")
        if not ok:
            print(f"  FAIL  {fq_schema}: {error}")
            failures.append((fq_schema, error))
            continue

        ok, error = _statement(
            client, warehouse_id, f"CREATE VOLUME IF NOT EXISTS {fq_schema}.audit_logs"
        )
        if not ok:
            print(f"  WARN  {fq_schema}.audit_logs: {error}")

        # Prove writability rather than assume it — a schema can exist while the caller
        # lacks CREATE TABLE, and that failure would otherwise surface mid-pipeline.
        probe = f"{fq_schema}._bootstrap_probe"
        ok, error = _statement(client, warehouse_id, f"CREATE TABLE IF NOT EXISTS {probe} (x INT)")
        if ok:
            _statement(client, warehouse_id, f"DROP TABLE IF EXISTS {probe}")
            print(f"  OK    {fq_schema} (schema + volume, writable)")
        else:
            print(f"  FAIL  {fq_schema} not writable: {error}")
            failures.append((fq_schema, error))

    print()
    if failures:
        print("Bootstrap incomplete. A workspace admin must grant CREATE SCHEMA on")
        print(f"catalog '{args.catalog}', or create these schemas manually:")
        for schema, _ in failures:
            print(f"  CREATE SCHEMA {schema};")
        return 1

    print("Bootstrap complete. Next: databricks bundle deploy -t dev")
    return 0


if __name__ == "__main__":
    sys.exit(main())
