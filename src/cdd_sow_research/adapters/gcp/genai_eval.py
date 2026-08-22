"""Gen AI evaluation adapter (EvaluationGatePort, A4, rule R5).

Backs the domain ``EvaluationGatePort`` with the **Gen AI evaluation service** on the
Gemini Enterprise Agent Platform (reached through the unified GenAI Client in the Vertex
AI SDK: ``vertexai.Client(...).evals``). It scores a golden dataset on the B1 metrics
(source-of-wealth groundedness, risk-band accuracy, citation accuracy, PII safety) and
maps the result onto a domain :class:`EvalReport`. ``gate`` is the promotion check.

All Vertex AI SDK imports are lazy so the on-prem / test profile imports this module
without ``google-cloud-aiplatform[evaluation]`` installed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...config import Settings
from ...domain.models import EvalMetricResult, EvalReport

_THRESHOLDS: dict[str, float] = {
    "sow_groundedness": 0.80,
    "risk_band_accuracy": 0.80,
    "citation_accuracy": 0.90,
    "pii_safety": 0.99,
}


def _examples_submitted(dataset_path: str) -> int:
    """Rows submitted for scoring, counted from the dataset the service was pointed at.

    The evaluation service returns aggregate metric scores, not a row count, but
    :attr:`EvalReport.passed` needs one: it fails closed when nothing was scored, so a run
    that measured no examples cannot certify a promotion. An unreadable dataset counts 0,
    which fails the gate rather than asserting a quality it never checked.
    """
    try:
        with Path(dataset_path).open(encoding="utf-8") as handle:
            return sum(1 for line in handle if line.strip())
    except OSError:
        return 0


class GenAiEvalAdapter:
    """Run the promotion eval via the Gen AI evaluation service."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: Any | None = None

    def _get_client(self) -> Any:
        if self._client is None:
            import vertexai

            self._client = vertexai.Client(
                project=self._settings.project_id, location=self._settings.region
            )
        return self._client

    def evaluate(self, dataset_path: str) -> EvalReport:
        """Score ``dataset_path`` and return a domain EvalReport.

        # verify: https://docs.cloud.google.com/vertex-ai/generative-ai/docs/models/run-evaluation
        """
        client = self._get_client()
        result = client.evals.evaluate(dataset=dataset_path, metrics=list(_THRESHOLDS))
        return self._to_report(dataset_path, result)

    def gate(self, target: str) -> bool:
        """Promotion gate: True iff ``target`` clears every B1 threshold."""
        return self.evaluate(target).passed

    @staticmethod
    def _to_report(dataset_path: str, result: Any) -> EvalReport:
        summary = getattr(result, "summary_metrics", {}) or {}
        results: list[EvalMetricResult] = []
        for metric, threshold in _THRESHOLDS.items():
            score = float(summary.get(metric, 0.0) or 0.0)
            results.append(
                EvalMetricResult(
                    metric=metric, score=score, threshold=threshold, passed=score >= threshold
                )
            )
        return EvalReport(
            dataset=dataset_path,
            results=tuple(results),
            n_examples=_examples_submitted(dataset_path),
        )
