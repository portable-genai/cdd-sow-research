"""Remote-platform evaluation adapter - thin HTTP client to Hrz4.

At promotion Doc1's quality is checked against the shared **Hrz4 AI Quality / model-risk**
service (``model-quality-gate``). This adapter implements :class:`EvaluationGatePort`
against Hrz4's real, hardened contract (SPEC §6):

* ``evaluate`` -> ``POST /v1/evaluations {target, dataset_id, bundle}`` -> EvalReport.
* ``gate``     -> ``POST /v1/gate {target, dataset_id, bundle}`` -> ``{passed}``.

**Sourced from the shared ``agent-eval-kit`` commons.** The HTTP contract is
``agent_eval_kit.gate_client.PromotionGateClient``; this adapter configures it (the registered
``doc1-cdd-sow`` bundle, the reasoning model, and this repo's S2S auth headers) and re-raises its
errors as :class:`RemoteEvaluationError`, so callers handle a single error type.

It returns the client's report UNCHANGED. Rebuilding a locally declared ``EvalReport``
from three of its fields silently discards exactly the attested promotion evidence the
client has just validated: the run id, the dataset version and digest, the evaluator, the
schema version, the artifact references and the ``attested`` flag. The domain
``EvalReport`` IS ``agent_eval_kit.report.EvalReport``, so any such mapper is a lossy
identity function.
"""

from __future__ import annotations

from agent_eval_kit.gate_client import GateClientError, PromotionGateClient

from ...config import Settings
from ...domain.errors import CddError
from ...domain.models import EvalReport
from ...envread import setting_or_default
from . import _s2s

_DEFAULT_URL = "http://localhost:8084"

#: The registered Hrz4 metric bundle for this vertical (Hrz4 owns the metrics + bars).
_BUNDLE = "doc1-cdd-sow"
#: Prompt/agent version tag; bump when the prompt corpus changes, or source it from a registry.
_PROMPT_VERSION = "v1"


class RemoteEvaluationError(CddError):
    """Raised when the Hrz4 quality service returns a non-2xx response."""


class RemoteEvaluationAdapter:
    """HTTP client for the Hrz4 ``model-quality-gate`` service (via PromotionGateClient)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._base_url = _s2s.validate_base_url(
            setting_or_default("HRZ_QUALITY_URL", _DEFAULT_URL),
            service=type(self).__name__,
        )
        self._client = PromotionGateClient(
            self._base_url,
            bundle=_BUNDLE,
            model=settings.models.reasoning,
            prompt_version=_PROMPT_VERSION,
            # Propagate this repo's S2S bearer/signed-actor headers on every call.
            auth_headers=lambda: _s2s.headers(settings=self._settings, base_url=self._base_url),
        )

    def evaluate(self, dataset_path: str) -> EvalReport:
        """Score ``dataset_path`` via Hrz4 and return the client's report, evidence intact."""
        try:
            return self._client.evaluate(dataset_path)
        except GateClientError as exc:
            raise RemoteEvaluationError(str(exc)) from exc

    def gate(self, target: str) -> bool:
        """Promotion gate: True iff Hrz4 reports ``target`` passes."""
        try:
            return self._client.gate(target)
        except GateClientError as exc:
            raise RemoteEvaluationError(str(exc)) from exc
