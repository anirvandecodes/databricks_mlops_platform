"""Model validation rules for the credit-risk classifier.

Thresholds live in code, under review, rather than in a notebook cell — changing the bar a
model must clear is then a reviewable event with an author and a rationale.

Consumed by ``mlflow.evaluate`` in validation/ModelValidation.py.
"""
from mlflow.models import MetricThreshold


# Probability above which an applicant is classified as a likely default.
#
# NOT 0.5. Defaults are the minority class (~30% in Credit-G), so a 0.5 cutoff produces a
# model that almost never predicts default: an early run here scored precision 0.86 with
# recall 0.04 — it declined almost nobody and therefore caught almost no defaults, while
# looking excellent on precision and AUC. 0.30 trades some precision for materially
# better recall, which is the right direction when a missed default costs the full
# exposure and a false positive costs one declined application.
#
# Tuning this is a risk decision, not an engineering one, which is why it sits here beside
# the thresholds rather than inside the notebook.
DECISION_THRESHOLD = 0.30


def validation_thresholds():
    """Minimum quality bar a candidate must clear before it can be promoted.

    Chosen for a binary default classifier on Credit-G:

    * ``roc_auc`` 0.70 — below this the model has little ranking power over the ~30% base
      default rate, well under what a portfolio decision can rest on. Threshold-free, so
      it measures the model rather than the operating point.
    * ``precision_score`` 0.40 — bounds false positives, since every predicted default is
      a declined applicant and therefore lost revenue.
    * ``recall_score`` 0.40 — bounds false negatives. Without a recall floor a model can
      pass on precision alone by predicting "default" almost never, which is precisely the
      failure this platform observed before the decision threshold was corrected.

    Gating on precision *and* recall together is what makes the pair meaningful: either
    metric alone is trivially gamed by moving the threshold to an extreme.
    """
    return {
        "roc_auc": MetricThreshold(threshold=0.70, greater_is_better=True),
        "precision_score": MetricThreshold(threshold=0.40, greater_is_better=True),
        "recall_score": MetricThreshold(threshold=0.40, greater_is_better=True),
    }



def custom_metrics():
    """Extra metrics computed during evaluation.

    Empty by design: the credit-specific metric this platform governs on is PSI, which is
    a *drift* metric over live traffic rather than a single-model evaluation metric, so it
    lives in monitoring (``platform_utils.metrics``) instead.
    """
    return []


def evaluator_config():
    """Additional ``mlflow.evaluate`` evaluator configuration."""
    return {}
