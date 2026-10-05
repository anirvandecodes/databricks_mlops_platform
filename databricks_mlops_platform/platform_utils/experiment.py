"""Governance gate for starting a live A/B experiment.

A live A/B split serves the challenger (arm B) to a slice of real customers *before* it
has been promoted to champion. That is in direct tension with the platform's core promise
— no model reaches production without an automated quality bar and a named human's
approval — so *starting* a live experiment is gated exactly like a promotion, and for the
same reason: a not-yet-approved model must not silently begin serving traffic.

The gate is deliberately distinct from the promotion gate:

* **A separate tag.** Promotion reads ``approval_status``; the experiment reads
  ``ab_experiment_status``. Authorising a bounded, reversible experiment on 10% of traffic
  is a different, smaller decision than authorising a model as the full champion, and
  conflating the two tags would let one imply the other.
* **Defence in depth.** Arm B must also have passed the automated quality bar — a version
  whose ``validation_status`` is ``FAILED`` can never be an experiment arm, even if
  someone tags it. This mirrors the check the promotion ``ApprovalGate`` performs.

Ending an experiment (routing 100% back to champion) is intentionally **ungated**, the
same asymmetry as rollback: requiring sign-off to *stop* exposing customers to arm B would
only prolong that exposure.
"""
from typing import Optional, Tuple

# UC tag a reviewer sets on the arm-B version to authorise a live experiment. Distinct from
# the promotion tag (platform_utils.promotion.APPROVAL_TAG) on purpose — see module docstring.
AB_APPROVAL_TAG = "ab_experiment_status"
AB_APPROVED_VALUE = "approved"

# Audit decision codes, written to the same evidence trail as promotions/rollbacks.
DECISION_EXPERIMENT_STARTED = "AB_EXPERIMENT_STARTED"
DECISION_EXPERIMENT_ENDED = "AB_EXPERIMENT_ENDED"


class ABExperimentNotApproved(Exception):
    """Raised when a live experiment is started without the required approval."""


def is_experiment_approved(
    model_name: str, version: str, client=None
) -> Tuple[bool, str]:
    """Check whether an arm-B version is authorised to enter a live experiment.

    Two conditions must hold, and the failing one is named so a blocked start says why:

    1. The version carries ``ab_experiment_status=approved``.
    2. The version did not fail model validation — an unqualified model is never an
       experiment arm, regardless of any tag.

    :return: ``(approved, detail)``.
    """
    from platform_utils.promotion import get_client

    client = client or get_client()
    model_version = client.get_model_version(model_name, str(version))
    tags = model_version.tags or {}

    # Defence in depth: a candidate that failed the automated bar is never eligible, even
    # if it has somehow been tagged approved.
    if tags.get("validation_status") == "FAILED":
        return False, (
            f"Version {version} failed model validation and cannot be an experiment arm."
        )

    value = tags.get(AB_APPROVAL_TAG, "")
    if value.strip().lower() == AB_APPROVED_VALUE:
        approver = tags.get("approved_by", "<approver not recorded>")
        return True, f"Experiment approved by {approver}."
    if value:
        return False, (
            f"Experiment tag present but not approved (value: {value!r})."
        )
    return False, (
        f"No {AB_APPROVAL_TAG!r} tag on version {version}. A reviewer must set "
        f"{AB_APPROVAL_TAG}={AB_APPROVED_VALUE} on the challenger version to authorise a "
        "live A/B experiment."
    )


def assert_experiment_allowed(
    model_name: str,
    b_version: str,
    environment: str,
    approval_required: bool,
    client=None,
) -> str:
    """Enforce the experiment gate before a live split is enabled.

    In dev/staging ``approval_required`` is false, so experimentation runs unattended. In
    prod it is true, and an unapproved arm-B version raises rather than serving traffic —
    the gate is a hard technical control, not a convention.

    :return: A human-readable detail of why the experiment was allowed.
    :raises ABExperimentNotApproved: If approval is required and absent.
    """
    if not approval_required:
        return (
            f"Experiment gate disabled for environment {environment!r} "
            "(approval_required=false); live split enabled automatically."
        )

    approved, detail = is_experiment_approved(model_name, b_version, client=client)
    if not approved:
        raise ABExperimentNotApproved(
            f"\n{'=' * 70}\n"
            f"A/B EXPERIMENT BLOCKED — approval required in environment {environment!r}.\n"
            f"{'=' * 70}\n"
            f"Model  : {model_name}\n"
            f"Arm B  : v{b_version}\n\n"
            f"{detail}\n\n"
            f"To authorise, set this tag on the challenger version and re-run:\n"
            f"    {AB_APPROVAL_TAG} = {AB_APPROVED_VALUE}\n"
            f"    approved_by       = <reviewer identity>\n\n"
            f"Until then, all traffic continues to serve @champion.\n"
            f"{'=' * 70}"
        )
    return detail


def experiment_metrics(
    split_pct: int,
    salt: str,
    arm_b_alias: str,
    arm_b_version: str,
    champion_version: Optional[str],
) -> dict:
    """Assemble the experiment parameters recorded in the audit evidence package.

    Kept as a plain dict so it slots straight into ``audit.build_evidence`` as
    ``eval_metrics`` — the same evidence trail promotions and rollbacks use.
    """
    return {
        "traffic_split_pct": split_pct,
        "split_salt": salt,
        "arm_b_alias": arm_b_alias,
        "arm_b_version": str(arm_b_version),
        "champion_version": str(champion_version) if champion_version else None,
    }
