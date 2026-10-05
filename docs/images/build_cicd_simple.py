"""Simplified governed-MLOps CI/CD flow as an editable draw.io diagram.

One box per stage, single left-to-right spine, three gates called out.
"""
import sys

SKILL = "/Users/anirvan.sen/.vibe/marketplace/plugins/fe-specialized-agents/skills/drawio-diagram"
sys.path.insert(0, f"{SKILL}/scripts")
from generate_drawio import DrawioBuilder, load_icons

icons = load_icons()
b = DrawioBuilder(width=1500, height=560, icons_cache=icons)

b.add_title(40, 10, 1420, "Governed MLOps — CI/CD Flow (simplified)",
            subtitle="PR → merge → release · gated at each step · promotion just moves an alias")

FLOW = "#2C5F8A"
DEPLOY = "#2E7D32"
GATE = "#B8410E"
BLOCK = "#B71C1C"

Y = 150   # main spine
H = 90

# --- Spine: five stages left to right ---
n_pr = b.add_node(40, Y, 175, H, "Pull request", "indigo",
                  icon_name="github", parent="1")
n_code = b.add_node(285, Y, 185, H, "Stage 1 · CODE GATE\ntests + bundle validate", "indigo",
                    icon_name="github_actions", parent="1")
n_stg = b.add_node(540, Y, 185, H, "Staging CD\ntrain→validate→promote", "green",
                   icon_name="lakehouse", parent="1")
n_prod = b.add_node(795, Y, 195, H, "Production CD\ntrain → stage @challenger", "orange",
                    icon_name="mlflow", parent="1")
n_champ = b.add_node(1245, Y, 190, H, "@champion\nserves traffic", "teal",
                     icon_name="endpoint", parent="1")

# --- Gate (above prod, between candidate and champion) ---
n_gate = b.add_node(1050, 55, 190, 90, "Stage 2 · DEPLOYMENT GATE\nGitHub reviewer\nsees metrics, approves", "gov",
                    icon_name="github_actions", parent="1")

# --- Governance evidence (below champion) ---
n_uc = b.add_node(1050, 300, 190, 90, "Unity Catalog\napproval_status tag\n(Stage 3 · MODEL GATE)", "gov",
                  icon_name="unity_catalog", parent="1")
n_audit = b.add_node(1245, 300, 190, 90, "Audit evidence\nVolume JSON + Delta row", "yellow",
                     icon_name="delta_lake", parent="1")

# ============ EDGES ============
# spine
b.add_edge(n_pr, n_code, "open PR", stroke_color=FLOW, stroke_width=2,
           exit_x=1, exit_y=0.5, entry_x=0, entry_y=0.5)
b.add_edge(n_code, n_stg, "checks pass → merge", stroke_color=DEPLOY, stroke_width=2.5,
           font_style=1, exit_x=1, exit_y=0.5, entry_x=0, entry_y=0.5)
b.add_edge(n_stg, n_prod, "push release/**", stroke_color=DEPLOY, stroke_width=2.5,
           font_style=1, exit_x=1, exit_y=0.5, entry_x=0, entry_y=0.5)

# prod -> gate -> champion
b.add_edge(n_prod, n_gate, "metrics", stroke_color=GATE, stroke_width=2,
           exit_x=0.75, exit_y=0, entry_x=0.2, entry_y=1)
b.add_edge(n_gate, n_champ, "approve → promote", stroke_color=GATE, stroke_width=2.5,
           font_style=1, exit_x=1, exit_y=0.5, entry_x=0.5, entry_y=0)

# gate writes tag / reject
b.add_edge(n_gate, n_uc, "records approval", stroke_color=GATE, dashed=True,
           exit_x=0.5, exit_y=1, entry_x=0.5, entry_y=0)
b.add_edge(n_prod, n_champ, "reject → prod untouched", stroke_color=BLOCK, dashed=True,
           exit_x=1, exit_y=0.5, entry_x=0, entry_y=0.5)

# champion -> evidence + rollback
b.add_edge(n_champ, n_audit, "rollback (ungated, seconds)", stroke_color=BLOCK,
           stroke_width=2.5, font_style=1, exit_x=0.5, exit_y=1, entry_x=0.5, entry_y=0)

b.add_legend(40, 300, [
    {"color": FLOW, "label": "normal flow", "style": "swatch"},
    {"color": DEPLOY, "label": "deploy / promote", "style": "swatch"},
    {"color": GATE, "label": "gate / approval", "style": "swatch"},
    {"color": BLOCK, "label": "reject / rollback (dashed)", "style": "swatch"},
], title="Legend")

out = "/Users/anirvan.sen/Documents/codebase/databricks_mlops_platform/docs/images/cicd_flow_simple.drawio"
b.save(out)
b.print_validation()
print("saved:", out)
