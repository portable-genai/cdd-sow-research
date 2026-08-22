"""WP4: the platform OTLP tracer binds cleanly and never fails a request.

Under ``CDD_PROFILE=platform`` the container must bind a real tracer (not an accidental
fallback), and a tracing failure (e.g. the OTel SDK absent, or the collector unreachable)
must be swallowed so ``CddService.assess`` is never broken by observability.
"""

from __future__ import annotations

import pytest

from cdd_sow_research.adapters.platform.otlp_tracer import (
    OtlpTracerAdapter,
    _cloud_run_session,
    _trace_endpoint,
)
from cdd_sow_research.config import Settings
from cdd_sow_research.domain.models import TokenUsage

_OTLP_ENV = "OTEL_EXPORTER_OTLP_ENDPOINT"


def test_otlp_span_is_non_fatal_when_the_sdk_is_absent(monkeypatch):
    # Endpoint set -> the OTLP path; the OTLP SDK is not in the dev env, so exporting must
    # be swallowed (yield), never raise into the caller.
    monkeypatch.setenv(_OTLP_ENV, "http://collector.internal:4318")
    tracer = OtlpTracerAdapter(Settings())
    with tracer.span("cdd.assess", action="assess"):
        pass  # must reach here without an exception
    tracer.record_token_usage(TokenUsage(input_tokens=10, output_tokens=5), model="gemini")


def test_unset_endpoint_falls_back_to_cloud_trace(monkeypatch):
    # No endpoint -> an explicit Cloud Trace fallback (the workspace-standard path), which
    # constructs cleanly (its GCP imports are lazy).
    monkeypatch.delenv(_OTLP_ENV, raising=False)
    tracer = OtlpTracerAdapter(Settings())
    assert tracer._fallback is not None


def test_otlp_http_endpoint_includes_signal_path(monkeypatch):
    monkeypatch.setenv(_OTLP_ENV, "http://collector.internal:4318")
    tracer = OtlpTracerAdapter(Settings())
    assert tracer._endpoint == "http://collector.internal:4318/v1/traces"
    assert _trace_endpoint(tracer._endpoint) == tracer._endpoint


def test_cloud_run_collector_uses_runtime_id_token_audience(monkeypatch):
    monkeypatch.setenv(
        _OTLP_ENV,
        "https://observability-collector-abc-uc.a.run.app",
    )
    tracer = OtlpTracerAdapter(Settings())
    assert tracer._endpoint.endswith("/v1/traces")
    assert tracer._audience == "https://observability-collector-abc-uc.a.run.app"
    assert tracer._cloud_run_auth is True


def test_explicit_otlp_audience_and_auth_override(monkeypatch):
    monkeypatch.setenv(_OTLP_ENV, "https://collector.example.test/otlp")
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_AUDIENCE",
        "https://collector.internal.example",
    )
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_CLOUD_RUN_AUTH", "true")
    tracer = OtlpTracerAdapter(Settings())
    assert tracer._endpoint == "https://collector.example.test/otlp/v1/traces"
    assert tracer._audience == "https://collector.internal.example"
    assert tracer._cloud_run_auth is True


def test_cloud_run_session_attaches_google_signed_id_token(monkeypatch):
    # `requests` and google-auth are [gcp]-extra dependencies, so this one case is guarded
    # while the rest of the module keeps proving the SDK-absent behaviour on the dev gate.
    requests = pytest.importorskip("requests")
    id_token = pytest.importorskip("google.oauth2.id_token")

    calls = []
    monkeypatch.setattr(
        id_token,
        "fetch_id_token",
        lambda request, audience: calls.append(audience) or "signed-id-token",
    )
    session = _cloud_run_session("https://collector.example.test")
    request = requests.Request("POST", "https://collector.example.test/v1/traces").prepare()
    assert session.auth is not None
    session.auth(request)
    assert request.headers["Authorization"] == "Bearer signed-id-token"
    assert calls == ["https://collector.example.test"]


def test_platform_profile_binds_the_tracer(monkeypatch):
    from cdd_sow_research.config import Container

    monkeypatch.delenv(_OTLP_ENV, raising=False)
    container = Container(Settings.load("config/settings.yaml"))
    object.__setattr__(container.settings, "profile", "platform")  # frozen dataclass
    tracer = container._bind("tracer")
    assert isinstance(tracer, OtlpTracerAdapter)


def test_application_error_inside_otlp_span_is_not_swallowed(monkeypatch):
    monkeypatch.setenv(_OTLP_ENV, "http://collector.internal:4318")
    tracer = OtlpTracerAdapter(Settings())

    class _Span:
        def set_attribute(self, key, value):
            pass

    class _SpanContext:
        def __enter__(self):
            return _Span()

        def __exit__(self, exc_type, exc, tb):
            return False

    class _Tracer:
        def start_as_current_span(self, name):
            return _SpanContext()

    monkeypatch.setattr(tracer, "_get_tracer", lambda: _Tracer())
    with pytest.raises(RuntimeError, match="domain failure"), tracer.span("cdd.assess"):
        raise RuntimeError("domain failure")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
