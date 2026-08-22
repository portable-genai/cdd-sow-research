"""Cloud Trace tracer adapter (ObservabilityTracerPort, A5).

Backs the domain tracer with **Cloud Trace via OpenTelemetry**. Each
``tracer.span(name)`` opens an OTel span for a unit of CDD work; token usage is recorded
as span attributes for FinOps. Message-content capture is **OFF**: spans carry structure
and token counts, never the prompt/response text, so customer PII never reaches a trace.

All OpenTelemetry / GCP exporter imports are lazy so the on-prem / test profile imports
this module without them installed.
"""

from __future__ import annotations

from contextlib import AbstractContextManager, contextmanager
from typing import Any

from ...config import Settings
from ...domain.models import TokenUsage


class CloudTraceTracerAdapter:
    """Open Cloud Trace spans via OpenTelemetry (message content OFF)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._tracer: Any | None = None

    def _get_tracer(self) -> Any:
        if self._tracer is None:
            from opentelemetry import trace
            from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            provider = TracerProvider()
            provider.add_span_processor(
                BatchSpanProcessor(CloudTraceSpanExporter(project_id=self._settings.project_id))
            )
            trace.set_tracer_provider(provider)
            self._tracer = trace.get_tracer("cdd-sow-research")
        return self._tracer

    def span(self, name: str, **attributes: str) -> AbstractContextManager[None]:
        """Open a trace span; attributes are structural only (never content)."""

        @contextmanager
        def _cm():
            tracer = self._get_tracer()
            with tracer.start_as_current_span(name) as span:
                for key, value in attributes.items():
                    span.set_attribute(key, value)
                yield

        return _cm()

    def record_token_usage(self, usage: TokenUsage, model: str) -> None:
        """Record token/cost metrics on the current span for FinOps."""
        from opentelemetry import trace

        span = trace.get_current_span()
        span.set_attribute("llm.model", model)
        span.set_attribute("llm.input_tokens", usage.input_tokens)
        span.set_attribute("llm.output_tokens", usage.output_tokens)
        span.set_attribute("llm.thinking_tokens", usage.thinking_tokens)
