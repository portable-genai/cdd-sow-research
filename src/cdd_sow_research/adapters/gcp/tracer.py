"""Managed ObservabilityTracerPort: OpenTelemetry, built by the commons.

This adapter is deliberately thin. All the OpenTelemetry work lives in
``hex_service_kit.tracing`` (the ``otel`` extra), so the exporter choice, the Cloud Run
authentication the agent-observability collector requires, and the rule that a tracing fault never
becomes a request fault are implemented once for the whole fleet rather than per repo. It replaced
a hand-written tracer that exported straight to Cloud Trace, around the collector's redaction.

Spans go OTLP to the agent-observability collector named by ``OTEL_EXPORTER_OTLP_ENDPOINT``,
which redacts GenAI content before anything reaches a Google sink. There is no direct Cloud Trace
path: the commons refuses to build a tracer when that variable is unset or empty, so a ``gcp``
deployment without a collector fails loudly on its first span instead of exporting around the
redaction (decision D1 of the guardrail/registry/observability plan). journey-portal injects the
endpoint into every embedded API it deploys.

Message content is never put on a span here or by the commons: spans carry the structural
attributes the caller passes, and token usage as counts.

The commons import is lazy for the usual reason: the local and on-prem profiles import this
package with no cloud SDK installed, and ``hex_service_kit.tracing`` itself imports clean without
OpenTelemetry, but the extra may simply not be present.
"""

from __future__ import annotations

from contextlib import AbstractContextManager

from hex_service_kit.observability import ObservabilityTracerPort, TokenUsage

from ...config import Settings

#: The service name every span is attributed to in the trace backend and the topology view.
_SERVICE_NAME = "cdd-sow-research"


class CloudTracerAdapter:
    """Binds the tracer port to the commons OpenTelemetry implementation."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._delegate: ObservabilityTracerPort | None = None

    def _tracer(self) -> ObservabilityTracerPort:
        if self._delegate is None:
            from hex_service_kit.tracing import build_tracer  # noqa: PLC0415

            self._delegate = build_tracer(service=_SERVICE_NAME)
        return self._delegate

    def span(self, name: str, **attributes: str) -> AbstractContextManager[None]:
        return self._tracer().span(name, **attributes)

    def record_token_usage(self, usage: TokenUsage, model: str) -> None:
        self._tracer().record_token_usage(usage, model)
