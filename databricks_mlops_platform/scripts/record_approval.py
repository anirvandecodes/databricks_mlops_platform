"""Record a reviewer's approval on a model version.

Run by the ``promote`` job of the production CD workflow, after a human has approved the
GitHub ``production`` environment deployment. The GitHub click is the decision; this
script is what makes that decision durable and auditable inside Unity Catalog, so the
approval is visible to anyone inspecting the model rather than only in CI history.

Deliberately does *not* move any alias. It writes the approval tags and stops; promotion
remains the job of ApprovalGate -> ModelDeployment, which re-checks the tag. That keeps
one enforcement point for "may this version serve traffic" instead of two.

Usage:
    python scripts/record_approval.py \
        --model workspace.payments_prod.credit_risk_model \
        --version 3 \
        --approver someone@example.com
"""
import argparse
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, help="Three-level UC model name.")
    parser.add_argument(
        "--version",
        required=True,
        help=(
            "Model version to approve. A non-numeric value is treated as an alias to "
            "resolve, e.g. 'challenger'."
        ),
    )
    parser.add_argument(
        "--approver",
        required=True,
        help="Identity to record in the approved_by tag (the GitHub actor who approved).",
    )
    args = parser.parse_args()

    from mlflow import MlflowClient

    sys.path.insert(0, ".")
    from platform_utils.promotion import APPROVAL_TAG, APPROVED_VALUE, get_alias_version

    # Both URIs are passed explicitly — see the note in candidate_report.py: a client built
    # without them talks to a local sqlite registry, which here would mean recording an
    # approval that never reaches Unity Catalog.
    client = MlflowClient(tracking_uri="databricks", registry_uri="databricks-uc")

    version = args.version
    if not version.isdigit():
        # The workflow approves "whatever this run staged", and the run records that as an
        # alias. Resolving it here avoids threading a version number between jobs, where a
        # stale value would approve the wrong model. Checked before calling the registry so
        # an unassigned alias reports itself rather than surfacing a BAD_REQUEST on the
        # alias name being used as a version number.
        alias = version
        version = get_alias_version(args.model, alias, client=client)
        if not version:
            print(
                f"No @{alias} alias on {args.model}: there is no staged candidate to "
                "approve. Run the training workflow first.",
                file=sys.stderr,
            )
            return 1

    detail = client.get_model_version(args.model, str(version))
    tags = detail.tags or {}

    # A version that failed the automated bar must not become approvable by a click. The
    # gate checks this too; refusing here means the approval tag is never even written,
    # so the audit trail cannot show an approval for a model that should not have had one.
    if tags.get("validation_status") == "FAILED":
        print(
            f"{args.model} v{version} failed model validation and cannot be approved. "
            "Retrain before requesting approval.",
            file=sys.stderr,
        )
        return 1

    client.set_model_version_tag(args.model, str(version), APPROVAL_TAG, APPROVED_VALUE)
    client.set_model_version_tag(args.model, str(version), "approved_by", args.approver)

    print(f"Recorded approval on {args.model} v{version}")
    print(f"  {APPROVAL_TAG} = {APPROVED_VALUE}")
    print(f"  approved_by    = {args.approver}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
