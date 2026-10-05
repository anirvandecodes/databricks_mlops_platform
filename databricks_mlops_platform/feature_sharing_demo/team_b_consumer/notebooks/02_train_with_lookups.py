# Databricks notebook source
# Team B — train on Team A's features with point-in-time lookups, log with feature metadata.
#
#   spine (customer_id, as_of_date, label)  +  FeatureLookup(s) from feature_config.yaml
#     -> create_training_set: each row gets the feature values as of as_of_date (no leakage)
#     -> fe.log_model: the model remembers which tables/columns it needs, so scoring
#        (03_batch_score) looks them up itself — Team B never re-implements Team A's logic
#     -> register in UC, alias @Champion, and tag the version with its feature dependencies
#        (read by governance/find_feature_consumers when a producer plans a change)

# COMMAND ----------

# MAGIC %pip install -q databricks-feature-engineering pyyaml scikit-learn
# MAGIC %restart_python

# COMMAND ----------

# MAGIC %run ../../shared/feature_contract_utils

# COMMAND ----------

import json

import mlflow
from databricks.feature_engineering import FeatureEngineeringClient, FeatureLookup
from mlflow.tracking import MlflowClient
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import train_test_split
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

for w in ("feature_config_path", "catalog", "producer_schema", "consumer_schema"):
    dbutils.widgets.text(w, "")
variables = {k: dbutils.widgets.get(k).strip() for k in ("catalog", "producer_schema", "consumer_schema")}
cfg = load_yaml(dbutils.widgets.get("feature_config_path").strip(), variables)

model_name = cfg["model"]["name"]
label = cfg["model"]["label"]
specs = lookup_specs(cfg)
for s in specs:
    print(f"lookup {s['table_name']}: {s['feature_names']}")

# COMMAND ----------

fe = FeatureEngineeringClient()
spine = spark.table(cfg["spine_table"])
training_set = fe.create_training_set(
    df=spine,
    feature_lookups=[FeatureLookup(**s) for s in specs],
    label=label,
    exclude_columns=["customer_id", "as_of_date"],
)
pdf = training_set.load_df().toPandas()
X, y = pdf.drop(columns=[label]), pdf[label]
X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.25, random_state=42, stratify=y)

model = make_pipeline(StandardScaler(), LogisticRegression(max_iter=1000))
model.fit(X_train, y_train)
auc = roc_auc_score(y_test, model.predict_proba(X_test)[:, 1])
print(f"Features: {list(X.columns)}")
print(f"Holdout AUC: {auc:.4f}")

# COMMAND ----------

mlflow.set_registry_uri("databricks-uc")
user = spark.sql("SELECT current_user()").first()[0]
mlflow.set_experiment(f"/Users/{user}/feature_sharing_demo_team_b")
client = MlflowClient()

previous_auc = None
try:
    champ = client.get_model_version_by_alias(model_name, "Champion")
    previous_auc = client.get_run(champ.run_id).data.metrics.get("holdout_auc")
except Exception:
    champ = None

with mlflow.start_run(run_name="team_b_default_risk") as run:
    mlflow.log_metric("holdout_auc", auc)
    mlflow.log_dict(cfg["feature_dependencies"], "feature_dependencies.json")
    fe.log_model(
        model=model,
        artifact_path="model",
        flavor=mlflow.sklearn,
        training_set=training_set,
        registered_model_name=model_name,
        infer_input_example=True,
    )

version = max(int(v.version) for v in client.search_model_versions(f"name = '{model_name}'")
              if v.run_id == run.info.run_id)
deps = {s["table_name"]: s["feature_names"] for s in specs}
client.set_model_version_tag(model_name, version, "feature_dependencies", json.dumps(deps))
client.set_model_version_tag(model_name, version, "contract_versions",
                             json.dumps({d["table"]: d.get("contract_version") for d in cfg["feature_dependencies"]}))
client.set_registered_model_tag(model_name, "consumer_team", "team_b")
client.set_registered_model_alias(model_name, "Champion", version)

print(f"Registered {model_name} v{version} -> @Champion (AUC {auc:.4f})")
if previous_auc is not None:
    print(f"Previous @Champion v{champ.version}: AUC {previous_auc:.4f}  (delta {auc - previous_auc:+.4f})")

# COMMAND ----------

dbutils.notebook.exit(json.dumps({
    "model": model_name, "version": version, "alias": "Champion", "features": list(X.columns),
    "holdout_auc": round(auc, 4), "previous_champion_auc": None if previous_auc is None else round(previous_auc, 4),
}))
