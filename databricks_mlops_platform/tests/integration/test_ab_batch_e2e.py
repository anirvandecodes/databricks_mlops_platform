"""Level-2 integration tests for the batch A/B path: real Unity Catalog + MLflow + Spark.

These assert what the offline tier cannot:

* The Spark ``crc32`` cohort expression agrees with the pure-Python ``zlib.crc32`` mirror —
  the property that makes a customer's arm identical in a notebook and in a test.
* ``score_batch_ab`` splits one batch across two models, stamps each row with its arm and
  the version that produced it, and unions to exactly the input row count.

Run explicitly (needs a workspace, slower than the unit tier):

    MLOPS_TEST_PROFILE=<profile> pytest tests/integration/test_ab_batch_e2e.py -v
"""
import os
import uuid

import pytest

pytest.importorskip("mlflow")

CATALOG = os.environ.get("MLOPS_TEST_CATALOG", "workspace")
SCHEMA = os.environ.get("MLOPS_TEST_SCHEMA", "payments_dev")


# -- Spark crc32 == Python zlib.crc32 --------------------------------------------


def test_spark_crc32_matches_python_mirror(spark):
    """The cohort a customer gets must be the same in Spark and in pure Python."""
    from platform_utils.variants import assign_variant, variant_column_expr

    ids = [f"cust_{i}" for i in range(500)]
    df = spark.createDataFrame([(i,) for i in ids], "customer_id string")
    spark_arms = {
        r["customer_id"]: r["variant"]
        for r in df.withColumn("variant", variant_column_expr("customer_id", 30)).collect()
    }

    mismatches = [cid for cid in ids if spark_arms[cid] != assign_variant(cid, split_pct=30)]
    assert not mismatches, f"Spark/Python disagreed on {len(mismatches)} ids, e.g. {mismatches[:5]}"


# -- score_batch_ab across two models --------------------------------------------


@pytest.fixture
def two_arm_model(spark):
    """A registered model with two versions aliased champion (v1) and challenger (v2).

    The two return different constant scores so each arm's rows are distinguishable, and a
    single named feature column keeps the model signature aligned with the scoring table.
    """
    import mlflow
    import pandas as pd
    from mlflow import MlflowClient
    from mlflow.models import infer_signature
    from sklearn.dummy import DummyClassifier

    mlflow.set_registry_uri("databricks-uc")
    client = MlflowClient()

    name = f"{CATALOG}.{SCHEMA}.it_ab_model_{uuid.uuid4().hex[:8]}"
    features = pd.DataFrame({"f0": [0.0, 1.0]})
    versions = []
    for constant in (0, 1):
        with mlflow.start_run():
            model = DummyClassifier(strategy="constant", constant=constant).fit(
                features, [0, 1]
            )
            logged = mlflow.sklearn.log_model(
                model,
                artifact_path="model",
                registered_model_name=name,
                input_example=features,
                signature=infer_signature(features, model.predict(features)),
            )
            versions.append(str(logged.registered_model_version))

    from platform_utils.promotion import CHALLENGER, CHAMPION, set_alias

    set_alias(name, CHAMPION, versions[0], client=client)
    set_alias(name, CHALLENGER, versions[1], client=client)

    yield name, versions

    try:
        client.delete_registered_model(name)
    except Exception as exc:  # pragma: no cover - best-effort teardown
        print(f"teardown: could not delete {name}: {exc}")


def test_score_batch_ab_unions_both_arms(spark, two_arm_model):
    """Both arms are scored by their own model, labelled, and unioned to the full count."""
    # mlflow.pyfunc.spark_udf refuses to run over Databricks Connect without a prebuilt
    # environment (prebuilt_env_uri), which is what CI and local runs use. Inside a job the
    # UDF runs on the cluster as normal, and the staging integration run in bundle-ci
    # exercises exactly that path (batch_inference_job with A/B on).
    if type(spark).__module__.startswith("pyspark.sql.connect"):
        pytest.skip("spark_udf needs cluster-side execution; covered by the staging integration run")
    from platform_utils.naming import AssetNames
    from platform_utils.promotion import CHALLENGER, CHAMPION
    from platform_utils.variants import ARM_CHALLENGER, ARM_CHAMPION
    from deployment.batch_inference.predict import score_batch_ab

    name, versions = two_arm_model
    names = AssetNames(catalog=CATALOG, schema=SCHEMA, model=name.split(".")[-1])

    n = 400
    input_table = f"{CATALOG}.{SCHEMA}.it_ab_scoring_{uuid.uuid4().hex[:8]}"
    spark.createDataFrame(
        [(f"c{i}", float(i % 5)) for i in range(n)], "customer_id string, f0 double"
    ).write.mode("overwrite").saveAsTable(input_table)

    try:
        result = score_batch_ab(
            spark,
            input_table=input_table,
            arms=[
                (ARM_CHAMPION, names.model_uri(CHAMPION), versions[0]),
                (ARM_CHALLENGER, names.model_uri(CHALLENGER), versions[1]),
            ],
            split_pct=25,
            salt="test_salt",
        )
        rows = result.collect()

        assert len(rows) == n, "union must cover every input row exactly once"
        arms = {r["variant"] for r in rows}
        assert arms == {ARM_CHAMPION, ARM_CHALLENGER}

        # Each arm's rows must carry the version that arm was scored with.
        by_arm_versions = {}
        for r in rows:
            by_arm_versions.setdefault(r["variant"], set()).add(r["model_version"])
        assert by_arm_versions[ARM_CHAMPION] == {versions[0]}
        assert by_arm_versions[ARM_CHALLENGER] == {versions[1]}

        # Arm B share should be near the configured split.
        b_share = sum(1 for r in rows if r["variant"] == ARM_CHALLENGER) / n
        assert 0.15 < b_share < 0.35, f"arm-B share {b_share:.2f} far from 25%"
    finally:
        spark.sql(f"DROP TABLE IF EXISTS {input_table}")
