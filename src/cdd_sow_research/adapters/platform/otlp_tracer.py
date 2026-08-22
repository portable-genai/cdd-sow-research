"""Platform tracer adapter (ObservabilityTracerPort) : OTLP export with a Cloud Trace fallback.

The workspace-standard tracing path exports OpenTelemetry spans straight to Cloud Trace
(the gcp adapter). The platform deployment may instead front tracing with the Hrz5 OTel
collector: when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set, this adapter exports OTLP to that
endpoint; when it is unset (the common case, since the collector is optional infra) it
delegates to :class:`CloudTraceTracerAdapter`, so the ``platform`` profile always has a
real tracer and never an accidental fallback.

Tracing setup/export is best-effort and must never fail a request. Application errors raised
inside a span always propagate; they must never be mistaken for exporter failures. All
OpenTelemetry imports are lazy so the ``local`` / ``onprem`` / test profiles import this
module with no OTel SDK.
"""

from __future__ import annotations

import logging
from contextlib import AbstractContextManager, contextmanager, suppress
from typing import Any
from urllib.parse import urlparse

from ...config import Settings
from ...domain.models import TokenUsage
from ...envread import boolean_setting, optional_setting
from ..gcp.cloud_trace_tracer import CloudTraceTracerAdapter

_LOG = logging.getLogger(__name__)
_OTLP_ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_ENDPOINT"
_OTLP_AUDIENCE_ENV = "OTEL_EXPORTER_OTLP_AUDIENCE"
_OTLP_CLOUD_RUN_AUTH_ENV = "OTEL_EXPORTER_OTLP_CLOUD_RUN_AUTH"


def _trace_endpoint(endpoint: str) -> str:
    """Return the concrete OTLP/HTTP traces URL expected by the exporter."""
    base = endpoint.rstrip("/")
    return base if base.endswith("/v1/traces") else f"{base}/v1/traces"


def _cloud_run_audience(endpoint: str) -> str:
    parsed = urlparse(endpoint)
    return f"{parsed.scheme}://{parsed.netloc}"


def _cloud_run_session(audience: str) -> Any:
    """Build a requests session that obtains a fresh Cloud Run ID token per export."""
    import requests
    from google.auth.transport.requests import Request
    from google.oauth2 import id_token

    class _CloudRunIdTokenAuth(requests.auth.AuthBase):
        def __call__(self, request: Any) -> Any:
            request.headers["Authorization"] = (
                f"Bearer {id_token.fetch_id_token(Request(), audience)}"
            )
            return request

    session = requests.Session()
    session.auth = _CloudRunIdTokenAuth()
    return session


class OtlpTracerAdapter:
    """Export OTel spans via OTLP when an endpoint is configured, else via Cloud Trace."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        configured_endpoint = optional_setting(_OTLP_ENDPOINT_ENV) or ""
        self._endpoint = _trace_endpoint(configured_endpoint) if configured_endpoint else ""
        self._audience = optional_setting(_OTLP_AUDIENCE_ENV) or ""
        if configured_endpoint and not self._audience:
            self._audience = _cloud_run_audience(configured_endpoint)
        hostname = urlparse(configured_endpoint).hostname or ""
        self._cloud_run_auth = boolean_setting(_OTLP_CLOUD_RUN_AUTH_ENV) or hostname.endswith(
            ".run.app"
        )
        # No OTLP endpoint: the collector is optional infra, so fall back to the
        # workspace-standard direct-to-Cloud-Trace exporter (an intentional, documented
        # default, not a silent gcp fallback).
        self._fallback = None if self._endpoint else CloudTraceTracerAdapter(settings)
        self._tracer: Any | None = None

    def _get_tracer(self) -> Any:
        if self._tracer is None:
            from opentelemetry import trace
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            exporter_options: dict[str, Any] = {"endpoint": self._endpoint}
            if self._cloud_run_auth:
                exporter_options["session"] = _cloud_run_session(self._audience)
            provider = TracerProvider()
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(**exporter_options)))
            trace.set_tracer_provider(provider)
            self._tracer = trace.get_tracer("cdd-sow-research")
        return self._tracer

    def span(self, name: str, **attributes: str) -> AbstractContextManager[None]:
        """Open a span; structural attributes only (never message content). Non-fatal."""
        if self._fallback is not None:
            return self._fallback.span(name, **attributes)

        @contextmanager
        def _cm():
            span_cm: Any | None = None
            try:
                tracer = self._get_tracer()
                span_cm = tracer.start_as_current_span(name)
                span = span_cm.__enter__()
                for key, value in attributes.items():
                    span.set_attribute(key, value)
            except Exception as exc:  # noqa: BLE001 - tracing must never fail a request
                if span_cm is not None:
                    with suppress(Exception):
                        span_cm.__exit__(type(exc), exc, exc.__traceback__)
                _LOG.warning("OTLP tracing setup for %r failed (non-fatal): %s", name, exc)
                yield
                return

            assert span_cm is not None
            try:
                yield
            except BaseException as body_exc:
                try:
                    span_cm.__exit__(type(body_exc), body_exc, body_exc.__traceback__)
                except Exception as trace_exc:  # noqa: BLE001 - body error remains authoritative
                    _LOG.warning(
                        "OTLP tracing close for %r failed (non-fatal): %s", name, trace_exc
                    )
                raise
            else:
                try:
                    span_cm.__exit__(None, None, None)
                except Exception as exc:  # noqa: BLE001 - export/close is non-fatal
                    _LOG.warning("OTLP tracing close for %r failed (non-fatal): %s", name, exc)

        return _cm()

    def record_token_usage(self, usage: TokenUsage, model: str) -> None:
        """Record token/cost metrics on the current span for FinOps (best-effort)."""
        if self._fallback is not None:
            self._fallback.record_token_usage(usage, model)
            return
        try:
            from opentelemetry import trace

            span = trace.get_current_span()
            span.set_attribute("llm.model", model)
            span.set_attribute("llm.input_tokens", usage.input_tokens)
            span.set_attribute("llm.output_tokens", usage.output_tokens)
            span.set_attribute("llm.thinking_tokens", usage.thinking_tokens)
        except Exception as exc:  # noqa: BLE001 - FinOps metrics are best-effort
            _LOG.warning("OTLP token-usage record failed (non-fatal): %s", exc)
