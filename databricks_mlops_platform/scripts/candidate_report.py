"""Report the staged candidate's version and validation metrics.

Used by the production CD workflow to put the candidate's numbers in the run summary, so
the reviewer approving the deployment sees how the model performed without leaving GitHub.

Goes through the MLflow client rather than the Unity Catalog REST API on purpose: the UC
``/versions/{v}`` response omits tags (returns ``tags: null``), and its ``/aliases/{a}``
response returns the whole model-version object keyed ``version``, not ``version_num``.
The client normalises both.

Writes ``candidate_version=<n>`` to $GITHUB_OUTPUT when that variable is set, and the
markdown metric rows to stdout.

Usage:
    python scripts/candidate_report.py --model workspace.payments_prod.credit_risk_model
"""
import argparse
import os
import sys

PREFIX = "validation_"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, help="Three-level UC model name.")
    parser.add_argument(
        "--alias",
        default="challenger",
        help="Alias identifying the staged candidate (default: challenger).",
    )
    parser.add_argument(
        "--format",
        choices=("table", "inline"),
        default="table",
        help=(
            "table: markdown rows for the step summary. inline: one line, for a ::notice:: "
            "annotation, which cannot contain newlines."
        ),
    )
    args = parser.parse_args()

    from mlflow import MlflowClient

    sys.path.insert(0, ".")
    from platform_utils.promotion import get_alias_version

    # Both URIs are passed explicitly. Outside a Databricks notebook there is no ambient
    # workspace context, and MLflow 3 defaults the tracking URI to a local sqlite file —
    # so a client built without them silently creates an empty local registry and reports
    # that nothing is staged, rather than failing.
    client = MlflowClient(tracking_uri="databricks", registry_uri="databricks-uc")

    version = get_alias_version(args.model, args.alias, client=client)
    if not version:
        print(
            f"No @{args.alias} alias on {args.model}: nothing is staged for review.",
            file=sys.stderr,
        )
        return 1

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as handle:
            handle.write(f"candidate_version={version}\n")

    tags = client.get_model_version(args.model, str(version)).tags or {}
    rows = [(k, v) for k, v in tags.items() if k.startswith(PREFIX)]
    if not rows:
        # Not an error: a version can exist before ModelValidation has tagged it. The caller
        # decides whether missing metrics should block, so say so plainly and exit clean.
        print(
            "| _no validation metrics recorded_ | |"
            if args.format == "table"
            else "no validation metrics recorded"
        )
        return 0

    # Status first, then metrics alphabetically: the verdict is what a reviewer looks for.
    rows.sort(key=lambda kv: (kv[0] != f"{PREFIX}status", kv[0]))
    labelled = [(key[len(PREFIX) :].replace("_", " "), value) for key, value in rows]

    if args.format == "inline":
        # Annotations are single-line, so join rather than emit one row per metric.
        print(" | ".join(f"{label} {value}" for label, value in labelled))
    else:
        for label, value in labelled:
            print(f"| {label} | {value} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
