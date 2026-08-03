"""Model promotion, approval gating and rollback.

The whole promotion design rests on one idea: inference resolves models by **alias**, not
by version. ``models:/cat.schema.model@champion`` is what every scoring job loads. So
promoting a model and rolling one back are the same cheap metadata operation — re-point an
alias — with no redeploy, no pipeline rerun, and no downtime.

Aliases used:
    challenger — a freshly trained candidate; scored, evaluated, not yet serving
    champion   — the version currently serving production traffic
"""
from typing import Any, Dict, List, Optional, Tuple

CHALLENGER = "challenger"
CHAMPION = "champion"

# UC tag a reviewer sets (via the model-version UI or API) to record sign-off. Using a
# tag rather than a bespoke approval store means the approval is itself a governed,
# lineage-tracked UC object.
APPROVAL_TAG = "approval_status"
APPROVED_VALUE = "approved"


class ApprovalNotGranted(Exception):
    """Raised when a promotion is attempted without the required approval."""


def get_client():
    from mlflow import MlflowClient

    return MlflowClient()


def set_alias(model_name: str, alias: str, version: str, client=None) -> None:
    """Point ``alias`` at ``version``, replacing any existing assignment."""
    client = client or get_client()
    client.set_registered_model_alias(name=model_name, alias=alias, version=str(version))


def get_alias_version(model_name: str, alias: str, client=None) -> Optional[str]:
    """Return the version currently behind ``alias``, or ``None`` if unassigned."""
    client = client or get_client()
    try:
        return str(client.get_model_version_by_alias(model_name, alias).version)
    except Exception:
        # No such alias yet — the first deployment into an environment.
        return None


def register_challenger(model_name: str, version: str, client=None) -> None:
    """Tag a newly trained version as the challenger candidate."""
    set_alias(model_name, CHALLENGER, version, client=client)


def is_approved(model_name: str, version: str, client=None) -> Tuple[bool, str]:
    """Check whether a model version carries reviewer approval.

    :return: ``(approved, detail)`` where ``detail`` explains the verdict — surfaced in
        job output so a blocked promotion says *why* it was blocked.
    """
    client = client or get_client()
    model_version = client.get_model_version(model_name, str(version))
    tags = model_version.tags or {}
    value = tags.get(APPROVAL_TAG, "")
    if value.strip().lower() == APPROVED_VALUE:
        approver = tags.get("approved_by", "<approver not recorded>")
        return True, f"Approved by {approver}."
    if value:
        return False, f"Approval tag present but not approved (value: {value!r})."
    return False, (
        f"No {APPROVAL_TAG!r} tag on version {version}. A reviewer must set "
        f"{APPROVAL_TAG}={APPROVED_VALUE} on the model version to authorise promotion."
    )


def promote_to_champion(
    model_name: str,
    version: str,
    environment: str,
    approval_required: bool,
    client=None,
) -> Dict[str, Any]:
    """Promote a version to champion, enforcing the approval gate.

    In dev/staging ``approval_required`` is false so the pipeline runs unattended. In prod
    it is true, and an unapproved version raises rather than promoting — the gate is a
    hard technical control, not a convention.

    :return: Summary of the promotion, including the version displaced (needed to make
        rollback a single call).
    :raises ApprovalNotGranted: If approval is required and absent.
    """
    client = client or get_client()

    if approval_required:
        approved, detail = is_approved(model_name, version, client=client)
        if not approved:
            raise ApprovalNotGranted(
                f"Promotion of {model_name} v{version} to {CHAMPION} blocked in "
                f"{environment!r}. {detail}"
            )
        approval_detail = detail
    else:
        approval_detail = (
            f"Approval gate disabled for environment {environment!r} "
            "(approval_required=false); promoted automatically."
        )

    previous = get_alias_version(model_name, CHAMPION, client=client)
    if previous == str(version):
        return {
            "model_name": model_name,
            "version": str(version),
            "previous_champion": previous,
            "changed": False,
            "detail": f"Version {version} is already {CHAMPION}; nothing to do.",
        }

    set_alias(model_name, CHAMPION, version, client=client)

    # The challenger alias has served its purpose once the candidate is champion.
    # Leaving it pointed at the same version would misrepresent a live model as an
    # untested candidate.
    if get_alias_version(model_name, CHALLENGER, client=client) == str(version):
        try:
            client.delete_registered_model_alias(name=model_name, alias=CHALLENGER)
        except Exception as exc:  # pragma: no cover - non-fatal cleanup
            print(f"Warning: could not clear {CHALLENGER!r} alias: {exc}")

    return {
        "model_name": model_name,
        "version": str(version),
        "previous_champion": previous,
        "changed": True,
        "detail": approval_detail,
    }


def rollback_champion(
    model_name: str, target_version: Optional[str] = None, client=None
) -> Dict[str, Any]:
    """Re-point champion at a previously good version.

    With no ``target_version`` the most recent prior version that is not the current
    champion is chosen, which is the common incident case ("undo the last promotion").

    :raises ValueError: If no earlier version exists to roll back to.
    """
    client = client or get_client()
    current = get_alias_version(model_name, CHAMPION, client=client)

    if target_version is None:
        target_version = _previous_version(model_name, current, client=client)
        if target_version is None:
            raise ValueError(
                f"No prior version of {model_name} available to roll back to "
                f"(current champion: {current})."
            )

    if str(target_version) == str(current):
        return {
            "model_name": model_name,
            "rolled_back_to": str(target_version),
            "previous_champion": current,
            "changed": False,
            "detail": f"Version {target_version} is already {CHAMPION}.",
        }

    set_alias(model_name, CHAMPION, target_version, client=client)
    return {
        "model_name": model_name,
        "rolled_back_to": str(target_version),
        "previous_champion": current,
        "changed": True,
        "detail": (
            f"Rolled {CHAMPION} back from v{current} to v{target_version}. "
            "Serving and batch jobs pick this up on their next model resolution."
        ),
    }


def _previous_version(model_name: str, current: Optional[str], client) -> Optional[str]:
    """Highest registered version below the current champion."""
    versions: List[int] = []
    for mv in client.search_model_versions(f"name='{model_name}'"):
        try:
            versions.append(int(mv.version))
        except (TypeError, ValueError):
            continue
    if not versions:
        return None

    versions.sort(reverse=True)
    if current is None:
        return str(versions[0])
    current_int = int(current)
    for version in versions:
        if version < current_int:
            return str(version)
    return None
