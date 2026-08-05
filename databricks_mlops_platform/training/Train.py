# Databricks notebook source
##################################################################################
# Credit Risk Model Training Notebook
#
# Trains a LightGBM binary classifier on the UCI Credit-G dataset to predict default
# probability, then registers it to Unity Catalog as a *challenger* candidate. It does
# not promote anything — promotion is a separate, gated step (see deployment/approval).
#
# This notebook is the "Train" task of the model_training_job workflow defined in
# databricks_mlops_platform/resources/model-workflow-resource.yml.
#
# Parameters (set via job base_parameters):
#   env             - Environment name (dev/staging/prod).
#   catalog_name    - UC catalog holding this environment's ML assets.
#   schema_name     - UC schema holding this environment's ML assets.
#   model_name      - Unqualified registered-model name.
#   experiment_name - MLflow experiment path. Created if missing.
##################################################################################

# COMMAND ----------

# MAGIC %pip install -r ../requirements.txt

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Notebook arguments
import sys

# The bundle syncs the project root; adding it to sys.path lets notebooks import the
# shared platform_utils package the same way local tests do.
sys.path.append("..")

dbutils.widgets.dropdown("env", "dev", ["dev", "staging", "prod"], "Environment")
dbutils.widgets.text("catalog_name", "workspace", "UC catalog")
dbutils.widgets.text("schema_name", "mlops_dev", "UC schema")
dbutils.widgets.text("model_name", "credit_risk_model", "Model name")
dbutils.widgets.text("experiment_name", "", "MLflow experiment")

env = dbutils.widgets.get("env")
experiment_name = dbutils.widgets.get("experiment_name")

from platform_utils.naming import AssetNames

names = AssetNames(
    catalog=dbutils.widgets.get("catalog_name"),
    schema=dbutils.widgets.get("schema_name"),
    model=dbutils.widgets.get("model_name"),
)

print(f"env             = {env}")
print(f"model           = {names.model_name}")
print(f"training table  = {names.training_data}")
print(f"experiment      = {experiment_name}")

# COMMAND ----------

# DBTITLE 1,Configure MLflow against Unity Catalog
import mlflow

if experiment_name:
    mlflow.set_experiment(experiment_name)
# Registering into UC (rather than the workspace registry) is what gives the model
# lineage, grants and alias governance the promotion flow depends on.
mlflow.set_registry_uri("databricks-uc")

# COMMAND ----------

# DBTITLE 1,Load the Credit-G dataset
from sklearn.datasets import fetch_openml
from sklearn.preprocessing import LabelEncoder

print("Fetching UCI Credit-G dataset...")
data = fetch_openml("credit-g", version=1, as_frame=True, parser="auto")
df = data.frame.copy()

# Credit-G labels 'good'/'bad' creditworthiness. We invert to make the positive class
# "defaulted" (=1), because a credit-risk model's job is to score the risk of default,
# and every downstream threshold (PD bands, PSI, approval criteria) assumes that
# orientation.
df["class"] = (df["class"] == "bad").astype(int)

# LightGBM needs numeric input; label-encode the categorical columns. The encoder
# mappings are logged with the run so a scoring-time mismatch is diagnosable.
encoders = {}
for column in df.select_dtypes(include="category").columns:
    encoder = LabelEncoder()
    df[column] = encoder.fit_transform(df[column].astype(str))
    encoders[column] = list(encoder.classes_)

print(f"Loaded {len(df)} rows, {df.shape[1]} columns. Default rate: {df['class'].mean():.2%}")

# COMMAND ----------

# DBTITLE 1,Apply engineered features
from pyspark.sql import functions as F

from feature_engineering.features.credit_features import compute_features_fn

# Feature logic lives in a tested module, not in the notebook — the same code path runs
# in unit tests, so what trains here is what was verified in CI.
raw_sdf = spark.createDataFrame(df)
featured_sdf = compute_features_fn(raw_sdf)

# COMMAND ----------

# DBTITLE 1,Persist the training snapshot
# Written as a Delta table so validation, the drift baseline and the audit trail all
# reference one immutable, time-travellable version of the training input.
featured_sdf_tagged = featured_sdf.withColumn("env", F.lit(env)).withColumn(
    "ingested_at", F.current_timestamp()
)

(
    featured_sdf_tagged.write.mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable(names.training_data)
)

training_data_version = (
    spark.sql(f"DESCRIBE HISTORY {names.training_data} LIMIT 1").collect()[0]["version"]
)
print(f"Wrote {featured_sdf_tagged.count()} rows to {names.training_data} (v{training_data_version})")

# COMMAND ----------

# DBTITLE 1,Train / test split
import pandas as pd
from sklearn.model_selection import train_test_split

# Bands are for monitoring slices, not model inputs; the label is the target.
DROP_COLUMNS = ["class", "env", "ingested_at", "residence_band", "age_band"]

pdf = featured_sdf.toPandas()
X = pdf.drop(columns=[c for c in DROP_COLUMNS if c in pdf.columns])
y = pdf["class"]

# Stratified split: the default rate is ~30%, so an unstratified split can materially
# shift class balance between train and test and make AUC hard to compare across runs.
X_train, X_test, y_train, y_test = train_test_split(
    X, y, test_size=0.2, random_state=42, stratify=y
)
print(f"Train={len(X_train)}, Test={len(X_test)}, features={X.shape[1]}")

# COMMAND ----------

# DBTITLE 1,Train the classifier
import lightgbm as lgb
import mlflow.lightgbm
from sklearn.metrics import average_precision_score, roc_auc_score

mlflow.lightgbm.autolog()

with mlflow.start_run(run_name=f"credit_risk_{env}") as run:
    params = {
        "objective": "binary",
        "metric": ["auc", "binary_logloss"],
        "learning_rate": 0.05,
        "num_leaves": 63,
        "min_data_in_leaf": 20,
        "verbose": -1,
        "random_state": 42,
    }
    train_ds = lgb.Dataset(X_train, label=y_train)
    val_ds = lgb.Dataset(X_test, label=y_test, reference=train_ds)

    model = lgb.train(
        params,
        train_ds,
        num_boost_round=200,
        valid_sets=[val_ds],
        valid_names=["validation"],
        callbacks=[lgb.early_stopping(20), lgb.log_evaluation(50)],
    )

    y_pred = model.predict(X_test)
    auc = roc_auc_score(y_test, y_pred)
    pr_auc = average_precision_score(y_test, y_pred)
    mlflow.log_metric("test_auc", auc)
    mlflow.log_metric("test_pr_auc", pr_auc)
    # Recorded on the run so the approval reviewer can see exactly which data version
    # produced these metrics without leaving the MLflow UI.
    mlflow.log_param("training_data_version", training_data_version)
    mlflow.log_param("training_table", names.training_data)
    mlflow.log_dict(encoders, "categorical_encoders.json")
    print(f"Test AUC={auc:.4f}  PR-AUC={pr_auc:.4f}")

    # Registering inside the active run sets the version's run_id, which validation and
    # the audit evidence both rely on to trace a model back to its metrics.
    logged = mlflow.lightgbm.log_model(
        model,
        artifact_path="lgb_model",
        input_example=X_train.iloc[[0]],
        registered_model_name=names.model_name,
    )
    run_id = run.info.run_id
    model_version = logged.registered_model_version

print(f"Registered {names.model_name} version {model_version}")

# COMMAND ----------

# DBTITLE 1,Write the drift baseline
# The data profiling monitor compares live inference against this snapshot. It must be the
# training distribution of the version being promoted, so it is rewritten per training run
# rather than created once.
#
# The label column is named `ground_truth`, matching the inference log rather than the
# training table's `class`. Data profiling requires the baseline and monitored table
# to share the label column name — a mismatch fails monitor creation with
# "label_col cannot be found".
baseline_pdf = X_train.copy()
baseline_pdf["prediction"] = model.predict(X_train)
baseline_pdf["ground_truth"] = y_train.astype(float).values

baseline_sdf = spark.createDataFrame(baseline_pdf).withColumn(
    "model_version", F.lit(str(model_version))
)
# The monitor also compares on the timestamp column, so the baseline must carry one.
baseline_sdf = baseline_sdf.withColumn("scored_at", F.current_timestamp())

(
    baseline_sdf.write.mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable(names.baseline_table)
)
print(f"Wrote drift baseline to {names.baseline_table}")

# COMMAND ----------

# DBTITLE 1,Tag the candidate as challenger
from platform_utils.promotion import register_challenger

# The new version becomes a *candidate*, not the serving model. Nothing that reads
# @champion is affected by this training run — that separation is what makes the
# approval gate meaningful.
register_challenger(names.model_name, model_version)
print(f"Tagged version {model_version} as challenger. @champion is unchanged.")

# COMMAND ----------

# DBTITLE 1,Emit task values for downstream tasks
dbutils.jobs.taskValues.set("model_name", names.model_name)
dbutils.jobs.taskValues.set("model_version", str(model_version))
dbutils.jobs.taskValues.set("model_uri", f"models:/{names.model_name}/{model_version}")
dbutils.jobs.taskValues.set("run_id", run_id)
dbutils.jobs.taskValues.set("training_data_version", str(training_data_version))
dbutils.jobs.taskValues.set("test_auc", float(auc))
dbutils.jobs.taskValues.set("test_pr_auc", float(pr_auc))

dbutils.notebook.exit(f"models:/{names.model_name}/{model_version}")
