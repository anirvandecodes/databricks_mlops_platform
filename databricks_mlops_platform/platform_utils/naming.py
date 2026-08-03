"""Asset naming and addressing for the MLOps platform.

Every table, model, volume and function name in the platform is derived here rather
than hardcoded in pipeline code. That gives one place to change the governance layout
(schema-per-environment today, catalog-per-environment later) without touching any
notebook, and it makes the environment of an asset self-evident from its address.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class AssetNames:
    """Fully-qualified Unity Catalog names for one environment's ML assets.

    Constructed from the bundle variables ``catalog_name`` / ``schema_name`` so that
    dev, staging and prod resolve to different addresses from identical code.
    """

    catalog: str
    schema: str
    model: str = "credit_risk_model"

    # -- container -------------------------------------------------------------
    @property
    def fq_schema(self) -> str:
        return f"{self.catalog}.{self.schema}"

    def table(self, name: str) -> str:
        return f"{self.catalog}.{self.schema}.{name}"

    # -- models ----------------------------------------------------------------
    @property
    def model_name(self) -> str:
        """Three-level UC model name, as required by the UC model registry."""
        return f"{self.catalog}.{self.schema}.{self.model}"

    def model_uri(self, alias: str = "champion") -> str:
        """Alias-based model URI.

        Inference always resolves the model by alias, never by version number. That
        indirection is what makes rollback a metadata operation (re-point the alias)
        instead of a redeployment.
        """
        return f"models:/{self.model_name}@{alias}"

    # -- feature / training data ------------------------------------------------
    @property
    def customer_features(self) -> str:
        return self.table("customer_features")

    @property
    def credit_features(self) -> str:
        return self.table("credit_features")

    @property
    def training_data(self) -> str:
        return self.table("training_data")

    @property
    def scoring_input(self) -> str:
        return self.table("scoring_input")

    @property
    def ground_truth(self) -> str:
        """Observed outcomes, joined to predictions once labels mature."""
        return self.table("ground_truth_outcomes")

    # -- inference / monitoring -------------------------------------------------
    @property
    def raw_predictions(self) -> str:
        """Model output before business rules are applied."""
        return self.table("raw_model_predictions")

    @property
    def inference_log(self) -> str:
        """Append-only inference log; the table Lakehouse Monitoring is attached to."""
        return self.table("inference_log")

    @property
    def baseline_table(self) -> str:
        """Training-time distribution snapshot, used as the drift baseline."""
        return self.table("baseline_snapshot")

    @property
    def profile_metrics(self) -> str:
        """Monitor-generated profile metrics (created by Lakehouse Monitoring)."""
        return f"{self.inference_log}_profile_metrics"

    @property
    def drift_metrics(self) -> str:
        """Monitor-generated drift metrics (created by Lakehouse Monitoring)."""
        return f"{self.inference_log}_drift_metrics"

    # -- decision layer ---------------------------------------------------------
    @property
    def credit_decisions(self) -> str:
        """Final business decisions after policy rules are applied."""
        return self.table("credit_decisions")

    @property
    def fn_evaluate_eligibility(self) -> str:
        return self.table("evaluate_eligibility")

    @property
    def fn_calculate_credit_limit(self) -> str:
        return self.table("calculate_credit_limit")

    @property
    def fn_calculate_psi(self) -> str:
        return self.table("calculate_psi")

    # -- audit ------------------------------------------------------------------
    @property
    def audit_volume(self) -> str:
        """UC Volume holding immutable promotion evidence packages."""
        return self.table("audit_logs")

    @property
    def audit_volume_path(self) -> str:
        return f"/Volumes/{self.catalog}/{self.schema}/audit_logs"

    @property
    def audit_table(self) -> str:
        """Queryable index of promotion events (the Volume holds the full payload)."""
        return self.table("promotion_audit_log")


def from_widgets(dbutils, default_model: str = "credit_risk_model") -> AssetNames:
    """Build :class:`AssetNames` from notebook widgets set by the bundle.

    Notebooks receive ``catalog_name`` / ``schema_name`` as job parameters; resolving
    them through one helper keeps every notebook's addressing identical.
    """
    return AssetNames(
        catalog=dbutils.widgets.get("catalog_name"),
        schema=dbutils.widgets.get("schema_name"),
        model=_widget_or(dbutils, "model_name", default_model),
    )


def _widget_or(dbutils, name: str, fallback: str) -> str:
    try:
        value = dbutils.widgets.get(name)
    except Exception:
        return fallback
    return value or fallback
