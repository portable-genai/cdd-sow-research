"""The managed tracer exports only through the agent-observability collector.

Decision D1 of the guardrail/registry/observability plan. The hand-written tracer this replaced
exported straight to Cloud Trace, around the collector that deletes GenAI content attributes.
The commons tracer has no such path: with no collector endpoint it refuses the first span, so a
deployment that forgot the wiring fails loudly instead of leaking quietly.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from hex_service_kit.tracing import CollectorEndpointRequiredError

from cdd_sow_research.adapters.gcp.tracer import CloudTracerAdapter
from cdd_sow_research.config import Settings

_SETTINGS = Path(__file__).resolve().parents[2] / "config" / "settings.yaml"
_ADAPTER = "cdd_sow_research.adapters.gcp.tracer:CloudTracerAdapter"


def test_the_managed_profiles_bind_the_commons_tracer() -> None:
    bindings = yaml.safe_load(_SETTINGS.read_text(encoding="utf-8"))["adapters"]["tracer"]

    assert bindings["gcp"] == _ADAPTER
    assert bindings["platform"] == _ADAPTER


def test_a_span_with_no_collector_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    tracer = CloudTracerAdapter(Settings.load(_SETTINGS))

    with pytest.raises(CollectorEndpointRequiredError):
        tracer.span("unit.of.work")
