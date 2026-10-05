"""Generate the governed-MLOps CI/CD flow as an editable draw.io diagram.

Left-to-right columns:
  Dev  ->  GitHub triggers  ->  Stage 1 code gate  ->  Staging CD
       ->  Production CD (3 jobs)  ->  Stage 2 deployment gate
       ->  Promote  ->  Unity Catalog governance
Governance/model gate + rollback shown as a bottom banner.
"""
import sys

SKILL = "/Users/anirvan.sen/.vibe/marketplace/plugins/fe-specialized-agents/skills/drawio-diagram"
sys.path.insert(0, f"{SKILL}/scripts")
from generate_drawio import DrawioBuilder, load_icons

icons = load_icons()
b = DrawioBuilder(width=2500, height=1150, icons_cache=icons)

b.add_title(40, 10, 2400, "Governed MLOps — CI/CD Flow",
            subtitle="Three triggers · three enforcement gates · alias-based promotion in Unity Catalog")
b.add_banner(40, 70, 2400, "PROMOTION FLOW  →   code gate · deployment gate · model gate")

TOP = 130  # container top

# ---------------- Column 1: Developer ----------------
c_dev = b.add_container(40, TOP, 170, 130, "Developer", "sources")
n_dev = b.add_node(15, 45, 140, 70, "Data scientist\ndeploy -t dev", "white",
                   icon_name="python", parent=c_dev)

# ---------------- Column 2: GitHub triggers ----------------
c_gh = b.add_container(240, TOP, 180, 340, "GitHub — source & triggers", "cicd")
n_pr = b.add_node(18, 45, 145, 70, "Pull request", "indigo",
                  icon_name="github", parent=c_gh)
n_main = b.add_node(18, 140, 145, 70, "main\n(merge)", "indigo",
                    icon_name="github", parent=c_gh)
n_rel = b.add_node(18, 235, 145, 70, "release/**\n(push)", "indigo",
                   icon_name="github", parent=c_gh)

# ---------------- Column 3: Stage 1 code gate ----------------
c_s1 = b.add_container(450, TOP, 210, 400, "Stage 1 · CODE GATE  (required checks)", "cicd")
n_unit = b.add_node(20, 45, 170, 70, "Unit tests\n(offline logic)", "indigo",
                    icon_name="github_actions", parent=c_s1)
n_integ = b.add_node(20, 135, 170, 70, "Integration tests\n(real UC)", "indigo",
                     icon_name="github_actions", parent=c_s1)
n_val = b.add_node(20, 225, 170, 70, "Bundle validate\ndev · staging · prod", "indigo",
                   icon_name="github_actions", parent=c_s1)
n_stgci = b.add_node(20, 315, 170, 70, "Staging integration\nfull pipeline e2e", "indigo",
                     icon_name="github_actions", parent=c_s1)

# ---------------- Column 4: Staging CD ----------------
c_stg = b.add_container(690, TOP, 200, 220, "Staging CD  (unattended)", "compute")
n_stgdep = b.add_node(18, 45, 165, 70, "Deploy bundle\n-t staging", "green",
                      icon_name="deploy", parent=c_stg)
n_stgrun = b.add_node(18, 130, 165, 75, "features → train →\nvalidate → gate →\npromote → score", "green",
                      icon_name="lakehouse", parent=c_stg)

# ---------------- Column 5: Production CD (3 jobs) ----------------
c_prod = b.add_container(920, TOP, 640, 470, "Production CD  (release/** push)", "aiml")

# Job 1
c_j1 = b.add_container(20, 40, 180, 120, "1 · Deploy code (no model change)", "compute", parent=c_prod)
n_pdep = b.add_node(15, 40, 150, 70, "bundle deploy\n-t prod", "green",
                    icon_name="deploy", parent=c_j1)

# Job 2
c_j2 = b.add_container(220, 40, 400, 200, "2 · Train & stage candidate", "aiml", parent=c_prod)
n_train = b.add_node(15, 45, 165, 70, "Train → Validate →\nApprovalGate (halts)", "orange",
                     icon_name="mlflow", parent=c_j2)
n_cand = b.add_node(210, 45, 165, 70, "Register as\n@challenger", "orange",
                    icon_name="model_registry", parent=c_j2)
n_report = b.add_node(15, 120, 360, 68, "candidate_report → metrics to run summary", "yellow",
                      icon_name="python", parent=c_j2)

# Job 3
c_j3 = b.add_container(220, 260, 400, 180, "3 · Promote (approved)", "consumption", parent=c_prod)
n_rec = b.add_node(15, 45, 175, 70, "record_approval\n→ UC tag (named actor)", "purple",
                   icon_name="python", parent=c_j3)
n_promote = b.add_node(210, 45, 175, 70, "ApprovalGate re-checks\n→ move @champion", "teal",
                       icon_name="endpoint", parent=c_j3)

# ---------------- Column 6: Stage 2 deployment gate ----------------
c_gate = b.add_container(1585, TOP + 190, 180, 170, "Stage 2 · DEPLOYMENT GATE", "governance")
n_ghenv = b.add_node(15, 45, 150, 100, "GitHub 'production'\nenvironment\nrequired reviewer", "gov",
                     icon_name="github_actions", parent=c_gate)

# ---------------- Column 7: Unity Catalog governance ----------------
c_uc = b.add_container(1800, TOP, 200, 470, "Unity Catalog governance", "governance")
n_uctag = b.add_node(18, 45, 165, 80, "approval_status tag\n(Stage 3 · MODEL GATE)", "gov",
                     icon_name="unity_catalog", parent=c_uc)
n_champ = b.add_node(18, 150, 165, 80, "models:/…@champion\nserves traffic", "teal",
                     icon_name="endpoint", parent=c_uc)
n_audit = b.add_node(18, 255, 165, 80, "Audit evidence\nVolume JSON + Delta row", "yellow",
                     icon_name="delta_lake", parent=c_uc)

# ==================== EDGES ====================
FLOW = "#2C5F8A"
DEPLOY = "#2E7D32"
GATE = "#B8410E"
BLOCK = "#B71C1C"

# dev -> github
b.add_edge(n_dev, n_pr, "open PR", stroke_color=FLOW,
           exit_x=1, exit_y=0.5, entry_x=0, entry_y=0.5)

# PR -> stage1 (code gate)
b.add_edge(n_pr, n_unit, "", stroke_color=FLOW,
           exit_x=1, exit_y=0.5, entry_x=0, entry_y=0.5)
b.add_edge(n_unit, n_integ, "", stroke_color=FLOW,
           exit_x=0.5, exit_y=1, entry_x=0.5, entry_y=0)
b.add_edge(n_val, n_stgci, "", stroke_color=FLOW,
           exit_x=0.5, exit_y=1, entry_x=0.5, entry_y=0)

# code gate passes -> merge to main
b.add_edge(n_stgci, n_main, "checks pass → merge", stroke_color=DEPLOY, stroke_width=2.5,
           font_style=1, exit_x=1, exit_y=0.5, entry_x=1, entry_y=1, dashed=True)

# main -> staging CD
b.add_edge(n_main, n_stgdep, "on merge", stroke_color=DEPLOY,
           exit_x=1, exit_y=0.5, entry_x=0, entry_y=0.5)
b.add_edge(n_stgdep, n_stgrun, "", stroke_color=DEPLOY,
           exit_x=0.5, exit_y=1, entry_x=0.5, entry_y=0)

# main -> release branch -> prod CD
b.add_edge(n_main, n_rel, "cut release branch", stroke_color=FLOW,
           exit_x=0.5, exit_y=1, entry_x=0.5, entry_y=0)
b.add_edge(n_rel, n_pdep, "on push", stroke_color=DEPLOY, stroke_width=2.5, font_style=1,
           exit_x=1, exit_y=0.5, entry_x=0, entry_y=0.5)

# prod CD internal
b.add_edge(n_pdep, n_train, "", stroke_color=FLOW,
           exit_x=1, exit_y=0.5, entry_x=0, entry_y=0.5)
b.add_edge(n_train, n_cand, "", stroke_color=FLOW,
           exit_x=1, exit_y=0.5, entry_x=0, entry_y=0.5)
b.add_edge(n_train, n_report, "", stroke_color=FLOW, dashed=True,
           exit_x=0.5, exit_y=1, entry_x=0.2, entry_y=0)

# metrics -> deployment gate (reviewer decides)
b.add_edge(n_report, n_ghenv, "reviewer sees metrics", stroke_color=GATE, stroke_width=2.5,
           font_style=1, exit_x=1, exit_y=0.5, entry_x=0.5, entry_y=0)

# gate approve -> promote job
b.add_edge(n_ghenv, n_rec, "approve", stroke_color=GATE, stroke_width=2.5, font_style=1,
           exit_x=0, exit_y=0.5, entry_x=1, entry_y=0.5)
# gate reject -> prod untouched (champion unchanged)
b.add_edge(n_ghenv, n_champ, "reject → prod untouched", stroke_color=BLOCK, dashed=True,
           exit_x=1, exit_y=0.5, entry_x=0, entry_y=0.2)

# promote internal + write tag + move champion + audit
b.add_edge(n_rec, n_promote, "", stroke_color=DEPLOY,
           exit_x=1, exit_y=0.5, entry_x=0, entry_y=0.5)
b.add_edge(n_rec, n_uctag, "writes tag", stroke_color=GATE, dashed=True,
           exit_x=0.5, exit_y=0, entry_x=0, entry_y=1)
b.add_edge(n_cand, n_uctag, "staged", stroke_color=GATE, dashed=True,
           exit_x=1, exit_y=0.2, entry_x=0, entry_y=0.5)
b.add_edge(n_promote, n_champ, "alias move", stroke_color=DEPLOY, stroke_width=2.5,
           font_style=1, exit_x=1, exit_y=0.5, entry_x=0, entry_y=0.5)
b.add_edge(n_promote, n_audit, "writes evidence", stroke_color=FLOW, dashed=True,
           exit_x=1, exit_y=0.8, entry_x=0, entry_y=0.6)

# rollback — ungated, seconds
b.add_edge(n_champ, n_audit, "rollback_job (ungated, seconds)", stroke_color=BLOCK,
           stroke_width=2.5, font_style=1,
           exit_x=0.5, exit_y=1, entry_x=0.5, entry_y=0)

# ---------------- Legend ----------------
b.add_legend(40, 620, [
    {"color": FLOW, "label": "normal flow", "style": "swatch"},
    {"color": DEPLOY, "label": "deploy / promote", "style": "swatch"},
    {"color": GATE, "label": "gate / approval", "style": "swatch"},
    {"color": BLOCK, "label": "reject / rollback (dashed)", "style": "swatch"},
], title="Legend")

out = "/Users/anirvan.sen/Documents/codebase/databricks_mlops_platform/docs/images/cicd_flow.drawio"
b.save(out)
b.print_validation()
print("saved:", out)
