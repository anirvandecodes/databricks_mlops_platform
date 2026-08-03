"""Safe access to Databricks job task values.

``dbutils.jobs.taskValues.get`` raises ``ValueError`` when the upstream task did not run in
the current job run — which happens whenever a single task is re-run in isolation
(``bundle run <job> --only <task>``), the exact workflow an operator uses to retry an
approval after signing off. ``debugValue`` does not cover that case: it only applies
outside a job context.

Every downstream task therefore reads task values through this helper, so re-running one
task never fails on a missing upstream value and can instead fall back to reading the
current state from Unity Catalog.
"""
from typing import Any


def task_value(dbutils, task_key: str, value_key: str, default: Any = None) -> Any:
    """Read a task value, returning ``default`` when it is unavailable.

    Handles both the "upstream task did not run in this run" case (raises ValueError) and
    the "running interactively outside a job" case.
    """
    try:
        value = dbutils.jobs.taskValues.get(task_key, value_key, default=default)
    except Exception:
        # Covers ValueError for a missing key and any non-job execution context.
        return default
    return default if value is None else value


def resolve_candidate_version(dbutils, names, task_key: str = "Train") -> str:
    """Determine which model version the current task should act on.

    Resolution order, most to least authoritative:

    1. The task value written by the training task in this run.
    2. The version currently behind the ``challenger`` alias — the candidate awaiting
       promotion, which is the right answer when a single task is being re-run.

    :raises RuntimeError: If neither source yields a version, since silently guessing a
        version to promote would be the most dangerous possible failure mode.
    """
    version = task_value(dbutils, task_key, "model_version")
    if version:
        return str(version)

    from platform_utils.promotion import CHALLENGER, get_alias_version

    version = get_alias_version(names.model_name, CHALLENGER)
    if version:
        print(
            f"No '{task_key}' task value in this run; falling back to the "
            f"@{CHALLENGER} alias (v{version})."
        )
        return str(version)

    raise RuntimeError(
        f"Cannot determine which version to act on: no '{task_key}' task value in this "
        f"run and no @{CHALLENGER} alias on {names.model_name}. Run the training task "
        "first, or set the alias explicitly."
    )
