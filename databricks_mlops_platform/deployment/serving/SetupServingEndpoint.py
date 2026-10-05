# Databricks notebook source
##################################################################################
# Real-time Serving Endpoint Setup (A/B)
#
# Creates or updates a Model Serving endpoint that serves the credit-risk model in real
# time, optionally splitting traffic between two versions for an A/B experiment.
#
# Two things to understand about the split here versus the batch path:
#   * Serving's traffic_config splits requests STOCHASTICALLY by percentage. It cannot
#     route by hash(customer_id), so a customer calling repeatedly may land in either arm.
#     The batch path is deterministic per customer; this is not. The comparison notebook
#     records serving_mode so the two are never conflated.
#   * The endpoint pins CONCRETE versions (not aliases), so it must be re-synced whenever
#     @champion / @challenger move. This notebook is idempotent — re-running re-reads the
#     aliases and updates the config — so it is safe to wire downstream of promotion and
#     rollback.
#
# When ab_enabled=false, all traffic routes to the champion. That is also the disable /
# rollback state: re-run with ab_enabled=false to end an experiment (ungated).
#
# "SetupServing" task of serving_endpoint_job (resources/serving-resource.yml).
##################################################################################

# COMMAND ----------

# MAGIC %pip install -r ../../requirements.txt

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Notebook arguments
import sys

sys.path.append("../..")

dbutils.widgets.dropdown("env", "dev", ["dev", "staging", "prod"], "Environment")
dbutils.widgets.text("catalog_name", "workspace", "UC catalog")
dbutils.widgets.text("schema_name", "mlops_dev", "UC schema")
dbutils.widgets.text("model_name", "credit_risk_model", "Model name")
dbutils.widgets.dropdown("ab_enabled", "false", ["true", "false"], "A/B experiment enabled")
dbutils.widgets.text("ab_variant_b_alias", "challenger", "A/B arm-B alias")
dbutils.widgets.text("ab_traffic_split_pct", "10", "A/B traffic to arm B (%)")
dbutils.widgets.dropdown("approval_required", "false", ["true", "false"], "Approval required")

env = dbutils.widgets.get("env")
ab_enabled = dbutils.widgets.get("ab_enabled").strip().lower() == "true"
ab_variant_b_alias = dbutils.widgets.get("ab_variant_b_alias")
ab_split_pct = int(dbutils.widgets.get("ab_traffic_split_pct"))
approval_required = dbutils.widgets.get("approval_required").strip().lower() == "true"

from platform_utils.naming import AssetNames

names = AssetNames(
    catalog=dbutils.widgets.get("catalog_name"),
    schema=dbutils.widgets.get("schema_name"),
    model=dbutils.widgets.get("model_name"),
)

# COMMAND ----------

# DBTITLE 1,Resolve the versions behind the aliases
import mlflow

mlflow.set_registry_uri("databricks-uc")

from platform_utils.promotion import CHAMPION, get_alias_version

champion_version = get_alias_version(names.model_name, CHAMPION)
if not champion_version:
    raise RuntimeError(
        f"No @{CHAMPION} alias on {names.model_name}. Promote a version before serving."
    )

variant_b_version = get_alias_version(names.model_name, ab_variant_b_alias) if ab_enabled else None
run_ab = ab_enabled and bool(variant_b_version)

print(f"Endpoint : {names.serving_endpoint_name}")
print(f"Champion : v{champion_version}")
if ab_enabled and not variant_b_version:
    print(f"A/B requested but no @{ab_variant_b_alias} alias; serving champion-only.")
print(f"A/B mode : {'ON' if run_ab else 'OFF'}"
      + (f" — arm B @{ab_variant_b_alias} v{variant_b_version} at {ab_split_pct}%" if run_ab else ""))

# COMMAND ----------

# DBTITLE 1,Gate a live experiment
# Routing live traffic to arm B is the same consequential decision as in the batch path, so
# it passes through the same experiment gate (hard in prod) and writes the same audit
# evidence. Ending the experiment (ab_enabled=false) skips this — stopping exposure is never
# gated.
if run_ab:
    from platform_utils.experiment import (
        DECISION_EXPERIMENT_STARTED,
        assert_experiment_allowed,
        experiment_metrics,
    )

    detail = assert_experiment_allowed(
        model_name=names.model_name,
        b_version=variant_b_version,
        environment=env,
        approval_required=approval_required,
    )
    print(f"Experiment gate: {detail}")

    from platform_utils.audit import build_evidence, write_evidence

    spark.sql(f"CREATE VOLUME IF NOT EXISTS {names.audit_volume}")
    evidence = build_evidence(
        model_name=names.model_name,
        version=str(variant_b_version),
        approver=f"experiment-operator:{env}",
        environment=env,
        eval_metrics={
            **experiment_metrics(
                split_pct=ab_split_pct,
                salt="n/a-realtime",
                arm_b_alias=ab_variant_b_alias,
                arm_b_version=variant_b_version,
                champion_version=champion_version,
            ),
            "serving_mode": "realtime",
        },
        decision=DECISION_EXPERIMENT_STARTED,
    )
    write_evidence(spark, evidence, volume_path=names.audit_volume_path, audit_table=names.audit_table)

# COMMAND ----------

# DBTITLE 1,Build the endpoint config
from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import DatabricksError, ResourceDoesNotExist
from databricks.sdk.service.serving import (
    AiGatewayConfig,
    AiGatewayInferenceTableConfig,
    EndpointCoreConfigInput,
    Route,
    ServedEntityInput,
    TrafficConfig,
)

w = WorkspaceClient()

# Served-entity names are stable ("champion"/"challenger") so that updating versions
# re-points an existing route rather than churning the endpoint.
served_entities = [
    ServedEntityInput(
        name="champion",
        entity_name=names.model_name,
        entity_version=str(champion_version),
        scale_to_zero_enabled=True,
        workload_size="Small",
    )
]
routes = [Route(served_model_name="champion", traffic_percentage=100)]

if run_ab:
    served_entities.append(
        ServedEntityInput(
            name="challenger",
            entity_name=names.model_name,
            entity_version=str(variant_b_version),
            scale_to_zero_enabled=True,
            workload_size="Small",
        )
    )
    # Percentages must sum to 100.
    routes = [
        Route(served_model_name="champion", traffic_percentage=100 - ab_split_pct),
        Route(served_model_name="challenger", traffic_percentage=ab_split_pct),
    ]

endpoint_config = EndpointCoreConfigInput(
    name=names.serving_endpoint_name,
    served_entities=served_entities,
    traffic_config=TrafficConfig(routes=routes),
)

# Request/response logging via AI Gateway inference tables — the current mechanism, after
# legacy `auto_capture_config` inference tables were deprecated. This gives the real-time
# arm the same kind of inference history the batch path writes to inference_log.
ai_gateway = AiGatewayConfig(
    inference_table_config=AiGatewayInferenceTableConfig(
        enabled=True,
        catalog_name=names.catalog,
        schema_name=names.schema,
        table_name_prefix=names.serving_payload_prefix,
    )
)

# COMMAND ----------

# DBTITLE 1,Create or update the endpoint (tolerating an absent serving API)
# Model Serving may be unavailable on some tiers (e.g. Free Edition). As with the data
# profiling monitor, a capability gap is not a pipeline fault: exit cleanly rather than
# failing. Permission and configuration errors still raise.
def _serving_unsupported(err: DatabricksError) -> bool:
    text = str(err).lower()
    return "no api found" in text or "not enabled" in text or "not available" in text


# The AI Gateway inference table is a separate capability from serving itself: some tiers
# serve models but do not offer request logging. The endpoint (the actual A/B mechanism) is
# the requirement; payload logging is best-effort, attached in a separate step so its
# absence degrades to "no request log" rather than failing the whole setup.
def _inference_table_unsupported(err: DatabricksError) -> bool:
    text = str(err).lower()
    return "inference table" in text and ("not supported" in text or "not currently supported" in text)


# A serving endpoint takes minutes to converge after a config change. Re-running this job
# while a prior update is still in flight is harmless — the desired config is already being
# applied — so a "currently being updated" conflict is treated as a benign no-op rather than
# a failure, keeping the job idempotent under repeated runs (e.g. a promotion re-sync firing
# before the last one settled).
def _update_in_progress(err: DatabricksError) -> bool:
    return "currently being updated" in str(err).lower()


def _attach_inference_table():
    try:
        w.serving_endpoints.put_ai_gateway(
            name=names.serving_endpoint_name,
            inference_table_config=ai_gateway.inference_table_config,
        )
        print(f"Request logging enabled → {names.serving_payload_table}")
    except DatabricksError as err:
        if not _inference_table_unsupported(err):
            raise
        print(
            "  Note: AI Gateway inference tables are not supported on this workspace; the "
            "endpoint serves traffic but real-time requests are not logged. The batch "
            "inference_log is unaffected."
        )


try:
    try:
        existing = w.serving_endpoints.get(name=names.serving_endpoint_name)
    except ResourceDoesNotExist:
        existing = None

    # Create/update the endpoint (served versions + traffic split) first — that is the A/B
    # capability. The inference table is attached separately so an unsupported table does not
    # block the endpoint.
    if existing is None:
        w.serving_endpoints.create_and_wait(
            name=names.serving_endpoint_name,
            config=endpoint_config,
        )
        print(f"Created serving endpoint {names.serving_endpoint_name}")
    else:
        w.serving_endpoints.update_config_and_wait(
            name=names.serving_endpoint_name,
            served_entities=endpoint_config.served_entities,
            traffic_config=endpoint_config.traffic_config,
        )
        print(f"Updated serving endpoint {names.serving_endpoint_name}")

    _attach_inference_table()

    print("Traffic routes:")
    for route in routes:
        print(f"  {route.served_model_name}: {route.traffic_percentage}%")

except DatabricksError as err:
    if _update_in_progress(err):
        print(
            "Endpoint is already applying an update from a previous run; the desired config "
            "is converging. Nothing to do."
        )
        dbutils.notebook.exit("SERVING_UPDATE_IN_PROGRESS")
    if not _serving_unsupported(err):
        raise
    print(
        "Model Serving is unavailable in this workspace "
        f"({w.config.host}); skipping endpoint setup.\n"
        f"  API response: {err}\n"
        "  Batch A/B and the governance chain are unaffected."
    )
    dbutils.notebook.exit("SERVING_UNSUPPORTED")

# COMMAND ----------

dbutils.notebook.exit(f"SERVING_READY:{'ab' if run_ab else 'champion_only'}")
