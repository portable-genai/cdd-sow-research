"""Local tracer adapter (ObservabilityTracerPort) — no-op spans.

The ``local`` profile's stand-in for **Cloud Trace via OpenTelemetry**: ``span`` is a
``contextlib.nullcontext`` and ``record_token_usage`` is a no-op, so domain code that
wraps work in ``tracer.span(...)`` runs unchanged with no observability backend wired up.
SDK-free and unconditional. Token usage is captured in memory for optional local
inspection (FinOps); message content is never captured (P-04).
"""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext

from ...config import Settings
from ...domain.models import TokenUsage


class LocalNoopTracerAdapter:
    """No-op tracer: nullcontext spans; token usage kept in memory for inspection."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.token_usage: list[tuple[TokenUsage, str]] = []

    def span(self, name: str, **attributes: str) -> AbstractContextManager[None]:
        # No-op span: domain code wrapping work in tracer.span(...) runs unchanged.
        return nullcontext()

    def record_token_usage(self, usage: TokenUsage, model: str) -> None:
        # Captured locally for optional FinOps inspection; no backend, never fatal.
        self.token_usage.append((usage, model))
