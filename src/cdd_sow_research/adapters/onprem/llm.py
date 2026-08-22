"""On-prem placeholder for ``LLMPort`` — the sovereign target.

One of the reversibility (P-02, P-12) migration placeholders: in the managed profile
this port binds to the Gemini adapter; switching ``profile`` to ``onprem`` rebinds it
here. The adapter constructs cleanly with **no external dependencies** and structurally
satisfies the same Protocol as the managed adapter, so the contract tests prove
interface parity. Porting B1 on-premise is *only* a matter of filling these bodies in:
the domain orchestration and service callers do not change.
"""

from __future__ import annotations

from ...config import Settings
from ...domain.models import LlmRequest, LlmResponse

_MESSAGE = (
    "On-prem LLMPort adapter is a migration placeholder; implement against your "
    "on-premise platform. Core domain logic is unchanged."
)


class OnPremLLMAdapter:
    """Placeholder LLM adapter for the on-prem profile."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def generate(self, request: LlmRequest) -> LlmResponse:
        raise NotImplementedError(_MESSAGE)

    def classify(self, text: str, labels: list[str]) -> str:
        raise NotImplementedError(_MESSAGE)
