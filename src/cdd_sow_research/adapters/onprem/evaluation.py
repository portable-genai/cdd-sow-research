"""On-prem placeholder for ``EvaluationGatePort`` — the sovereign target.

One of the reversibility (P-02, P-12) migration placeholders: in the managed profile
this port binds to the Gen AI evaluation-service adapter; switching ``profile`` to
``onprem`` rebinds it here. The adapter constructs cleanly with **no external
dependencies** and structurally satisfies the same Protocol as the managed adapter, so
the contract tests prove interface parity. ``evaluate`` / ``gate`` deliberately raise
rather than returning a vacuously-passing report: an unimplemented promotion gate must
never wave a model through unevaluated (rule R5). Filling these bodies in is the only
change required.
"""

from __future__ import annotations

from ...config import Settings
from ...domain.models import EvalReport

_MESSAGE = (
    "On-prem EvaluationGatePort adapter is a migration placeholder; implement against "
    "your on-premise platform. Core domain logic is unchanged."
)


class OnPremEvalAdapter:
    """Placeholder evaluation-gate adapter for the on-prem profile."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def evaluate(self, dataset_path: str) -> EvalReport:
        raise NotImplementedError(_MESSAGE)

    def gate(self, target: str) -> bool:
        raise NotImplementedError(_MESSAGE)
