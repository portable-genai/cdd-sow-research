"""Observability ports — the A5 (audit/trace) and A4 (eval gate) concerns.

Primary GCP adapters: **Cloud Logging locked WORM bucket** for immutable audit (rule
R2), **Cloud Trace via OpenTelemetry** for the reasoning-loop traces (message content
capture OFF so customer PII never reaches a span), and the **Gen AI evaluation
service** plus the **A4 promotion gate** for model-risk (rule R5).

**Two of the three ports here are not declared in this repository.**
``ObservabilityTracerPort`` (with its ``TokenUsage`` value type) comes from
``hex_service_kit.observability`` and ``EvaluationGatePort`` from ``agent_eval_kit``. Hand-copied
Protocol bodies are how a fleet of repositories ends up with subtly different definitions of the
same contract: one drops the eval port entirely, another drops its ``gate`` method, which is the
half that can refuse a promotion. Re-exporting means there is one definition to fix, and the
parity test asserts object identity rather than structural conformance, because a look-alike
copy satisfies a ``runtime_checkable`` Protocol.

``AuditSinkPort`` stays declared here: it is typed in this repository's own vocabulary
(:class:`~cdd_sow_research.domain.models.AuditEvent`), so it is not a shared contract.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from agent_eval_kit import EvaluationGatePort
from hex_service_kit.observability import ObservabilityTracerPort, TokenUsage

from ..domain.models import AuditEvent


@runtime_checkable
class AuditSinkPort(Protocol):
    def record(self, event: AuditEvent) -> None:
        """Write an immutable, already-redacted audit record (WORM)."""
        ...

    def record_once(self, event_id: str, event: AuditEvent) -> None:
        """Write once under a stable idempotency key."""
        ...


__all__ = ["AuditSinkPort", "EvaluationGatePort", "ObservabilityTracerPort", "TokenUsage"]
