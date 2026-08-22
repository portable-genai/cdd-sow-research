"""On-prem placeholder for ``ObservabilityTracerPort`` — the sovereign target.

One of the reversibility (P-02, P-12) migration placeholders. Unlike the safety/audit
stubs, tracing is **non-essential to correctness**, so this placeholder makes tracing
simply *absent* on-prem rather than fatal:

* ``span`` returns a no-op context manager (``contextlib.nullcontext``), so domain code
  that wraps work in ``tracer.span(...)`` runs unchanged: the block just executes with
  no span recorded.
* ``record_token_usage`` is a no-op, so FinOps metric emission silently does nothing.

This lets the whole pipeline run on an on-premise install with no observability backend
wired up yet, instead of crashing on a missing tracer. A real on-prem tracer can be
filled in later without touching domain logic. The adapter constructs cleanly with **no
external dependencies** and structurally satisfies the same Protocol as the managed
adapter.
"""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext

from ...config import Settings
from ...domain.models import TokenUsage


class OnPremTracerAdapter:
    """Placeholder tracer adapter for the on-prem profile.

    Tracing is absent (no-op) rather than fatal so the pipeline runs with no
    observability backend wired up; a real tracer can be filled in later.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def span(self, name: str, **attributes: str) -> AbstractContextManager[None]:
        # No-op span: domain code wrapping work in tracer.span(...) runs unchanged.
        return nullcontext()

    def record_token_usage(self, usage: TokenUsage, model: str) -> None:
        # No-op: FinOps metric emission is simply absent on-prem until wired up.
        return None
