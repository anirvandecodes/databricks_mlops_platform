"""CI/CD flow for the governed MLOps reference platform.

Three GitHub triggers → three enforcement gates → alias-based promotion in Unity Catalog.
Rendered with mingrammer/diagrams (Graphviz).
"""
import os
from diagrams import Diagram, Cluster, Edge
from diagrams.custom import Custom
from diagrams.onprem.vcs import Github
from diagrams.onprem.ci import GithubActions
from diagrams.onprem.client import User
from diagrams.programming.language import Python

ICONS = "/Users/anirvan.sen/.vibe/marketplace/plugins/fe-workflows/skills/fe-architecture-diagram/resources/icons"
DBX = f"{ICONS}/databricks"

# Palette
GATE = "#B8410E"      # gates / approvals — burnt orange
DEPLOY = "#1B7A43"    # deploys / promotion — green
FLOW = "#2C5F8A"      # normal flow — blue
BLOCK = "#9E1B32"     # the blocking / rollback path — red

graph_attr = {
    "splines": "spline",
    "nodesep": "0.55",
    "ranksep": "1.35",
    "pad": "0.6",
    "fontsize": "26",
    "fontname": "Helvetica-Bold",
    "bgcolor": "white",
    "dpi": "150",
    "labelloc": "t",
}
node_attr = {"fontsize": "13", "fontname": "Helvetica"}
edge_attr = {"fontsize": "12", "fontname": "Helvetica"}

with Diagram(
    "Governed MLOps — CI/CD Flow",
    show=False,
    filename="/Users/anirvan.sen/Documents/codebase/databricks_mlops_platform/docs/images/cicd_flow",
    outformat="png",
    direction="LR",
    graph_attr=graph_attr,
    node_attr=node_attr,
    edge_attr=edge_attr,
):

    dev = User("Data scientist\n(local IDE)")

    with Cluster("GitHub — source & triggers"):
        pr = Github("Pull request")
        main = Github("main\n(merge)")
        release = Github("release/**\n(push)")

    # ---- Stage 1: code gate (runs on every PR) ----
    with Cluster("Stage 1 · CODE GATE  (branch protection — required checks)"):
        unit = GithubActions("Unit tests\n(offline logic)")
        integ = GithubActions("Integration tests\n(real UC)")
        validate = GithubActions("Bundle validate\ndev · staging · prod")
        staging_ci = GithubActions("Staging integration\nfull pipeline e2e")
        unit >> Edge(color=FLOW) >> integ
        validate >> Edge(color=FLOW) >> staging_ci

    # ---- Staging CD (on merge to main, unattended) ----
    with Cluster("Staging CD  (unattended — approval_required=false)"):
        stg_deploy = Custom("Deploy bundle\n-t staging", f"{DBX}/workspace.png")
        stg_run = Custom("features → train → validate\n→ gate → promote → score", f"{DBX}/lakehouse.png")
        stg_deploy >> Edge(color=DEPLOY) >> stg_run

    # ---- Prod CD (on release/** push) — the three-job story ----
    with Cluster("Production CD  (release/** push)"):
        with Cluster("1 · Deploy code (no model change)"):
            prod_deploy = Custom("bundle deploy -t prod\nvalidate + deploy", f"{DBX}/workspace.png")
        with Cluster("2 · Train & stage candidate"):
            prod_train = Custom("Train → Validate →\nApprovalGate (halts)", f"{DBX}/model_serving.png")
            candidate = Custom("Register as\n@challenger", f"{DBX}/delta_lake.png")
            report = Python("candidate_report\nmetrics → run summary")
            prod_train >> Edge(color=FLOW) >> candidate
            prod_train >> Edge(color=FLOW, style="dashed") >> report

        with Cluster("Stage 2 · DEPLOYMENT GATE"):
            gh_env = GithubActions("GitHub 'production'\nenvironment\nrequired reviewer")

        with Cluster("3 · Promote (approved)"):
            rec = Python("record_approval\n→ UC tag (named actor)")
            promote = Custom("ApprovalGate re-checks tag\n→ move @champion", f"{DBX}/model_serving.png")
            rec >> Edge(color=DEPLOY) >> promote

    # ---- Governance / evidence ----
    with Cluster("Unity Catalog governance"):
        uc_tag = Custom("approval_status tag\n(Stage 3 · MODEL GATE)", f"{DBX}/unity_catalog.png")
        champion = Custom("models:/…@champion\nserves traffic", f"{DBX}/model_serving.png")
        audit = Custom("Audit evidence\nVolume JSON + Delta row", f"{DBX}/delta_lake.png")

    # ============ edges ============
    dev >> Edge(color=FLOW, label="deploy -t dev") >> pr
    dev >> Edge(color=FLOW, label="open PR") >> unit
    dev >> Edge(color=FLOW) >> validate

    # PR merges after code gate passes
    staging_ci >> Edge(color=DEPLOY, label="checks pass → merge") >> main
    integ >> Edge(color=DEPLOY, style="dashed") >> main

    main >> Edge(color=DEPLOY, label="on merge") >> stg_deploy
    main >> Edge(color=FLOW, label="cut release branch") >> release

    release >> Edge(color=DEPLOY, label="on push") >> prod_deploy
    prod_deploy >> Edge(color=FLOW) >> prod_train

    # gate flow
    report >> Edge(color=GATE, label="reviewer sees metrics") >> gh_env
    gh_env >> Edge(color=GATE, label="approve") >> rec
    gh_env >> Edge(color=BLOCK, style="dashed", label="reject → prod untouched") >> champion

    rec >> Edge(color=GATE) >> uc_tag
    promote >> Edge(color=DEPLOY, label="alias move") >> champion
    promote >> Edge(color=FLOW, style="dashed", label="writes evidence") >> audit
    candidate >> Edge(color=GATE, style="dashed") >> uc_tag

    # rollback — ungated, seconds
    champion >> Edge(color=BLOCK, label="rollback_job (ungated, seconds)", style="bold") >> audit
