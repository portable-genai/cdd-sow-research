"""Sampling is decided per call: pinned where the output is compared, free where it is prose.

History. The paired demonstration compares what `pairing.py` calls the DETERMINISTIC dossier,
and the comparison is only meaningful if each profile returns the same answer for the same
inputs. On 2026-08-26 the deployment did not: two runs of the identical case, same subject and
same single-document corpus, minutes apart, returned `score` 0.5 then 0.0, `confidence` 0.4 then
1.0, and four scorecard factors then none. The shared builder defaulted to `temperature=0.2`, so
every grounded call sampled, and from then until 2026-09-23 this file pinned 0.0 on the type and
on the builder, for every call, prose included.

What changed (owner decision, 2026-09-23). Temperature is PINNED (0.0) only where
reproducibility matters: extraction, classification, scoring, anything whose output is compared
or feeds a deterministic check. It is FREE for drafting, narration and judging, and free means
the parameter is not sent at all: Opus 5 and Fable 5 reject it, so 1.0 is not "free".

So the default is now ``None`` on the type and the builder, and each call site states its choice.
The assertions drive the REAL services with a recording model and read the requests they built,
because a source-text check passes against a fresh copy of the request-building code.

**Temperature 0 is not a promise of determinism** and nothing here asserts one: a hosted model can
still vary across batching and revisions. It is the strongest thing the caller controls.
"""

from __future__ import annotations

import inspect
import sys
from datetime import date
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from tests.conftest import (
    FakeAdverseMedia,
    FakeAudit,
    FakeCompliance,
    FakeExtraction,
    FakeGuardrail,
    FakeKnowledgeBase,
    FakeLLM,
    FakeRedaction,
    FakeRegistry,
    FakeTracer,
)

from cdd_sow_research.adapters.gcp.gemini_llm import GeminiLLMAdapter
from cdd_sow_research.adapters.local.monitoring_store import LocalMonitoringStoreAdapter
from cdd_sow_research.adapters.local.ownership_graph import LocalOwnershipGraphAdapter
from cdd_sow_research.config import Settings, build_container
from cdd_sow_research.domain import _grounded as g
from cdd_sow_research.domain import prompts
from cdd_sow_research.domain.kernel import LlmMessage, LlmRequest
from cdd_sow_research.domain.models import CaseInput, Subject, SubjectType
from cdd_sow_research.domain.services import CddService, PerpetualKycService, UboGraphService


class _Router:
    def route(self, *args: Any, **kwargs: Any) -> None:
        return None

    def route_monitoring(self, *args: Any, **kwargs: Any) -> None:
        return None

    def route_ownership(self, *args: Any, **kwargs: Any) -> None:
        return None


def _subject() -> Subject:
    return Subject(
        id="acme",
        name="Acme Holdings Pte Ltd (FICTIONAL)",
        type=SubjectType.ENTITY,
        jurisdiction="SG",
        tenant="demo-bank",
    )


def _dossier_requests() -> dict[str, LlmRequest]:
    """The requests one CDD dossier makes, keyed by which call built them."""
    from cdd_sow_research.domain import risk_service, sow_service

    llm = FakeLLM()
    CddService(
        extraction=FakeExtraction(),
        knowledge_base=FakeKnowledgeBase(),
        adverse_media=FakeAdverseMedia(),
        registry=FakeRegistry(),
        compliance=FakeCompliance(),
        llm=llm,
        guardrail=FakeGuardrail(),
        redaction=FakeRedaction(),
        tracer=FakeTracer(),
        audit=FakeAudit(),
    ).assess(CaseInput(subject=_subject()), "analyst@bank.test")
    by_schema = {
        id(risk_service._RISK_SCHEMA): "risk",
        id(sow_service._SOW_SCHEMA): "sow",
        id(sow_service._CRITIQUE_SCHEMA): "critique",
    }
    return {by_schema[id(r.response_schema)]: r for r in llm.requests}


def _narration_requests() -> dict[str, LlmRequest]:
    settings = Settings.load("config/settings.yaml")
    container = build_container(settings)
    llm = FakeLLM()
    UboGraphService.from_policy(
        settings.policy,
        ownership_graph=LocalOwnershipGraphAdapter(settings),
        review_router=_Router(),
        audit=FakeAudit(),
        tracer=container.tracer,
        redaction=container.redaction,
        llm=llm,
    ).resolve(_subject(), actor="tester", as_of=date(2026, 8, 7))
    PerpetualKycService.from_policy(
        settings.policy,
        sanctions=container.sanctions,
        adverse_media=container.adverse_media,
        registry=container.registry,
        store=LocalMonitoringStoreAdapter(settings),
        review_router=_Router(),
        audit=FakeAudit(),
        tracer=container.tracer,
        redaction=container.redaction,
        llm=llm,
    ).run(_subject(), actor="tester", principals=("tenant:demo-bank",), as_of=date(2026, 8, 5))
    by_prompt = {
        prompts.UBO_GRAPH_NARRATIVE_PROMPT: "ubo",
        prompts.PERPETUAL_KYC_NARRATIVE_PROMPT: "perpetual_kyc",
    }
    return {by_prompt[r.system_instruction]: r for r in llm.requests}  # type: ignore[index]


def test_the_type_and_the_builder_leave_sampling_free_by_default() -> None:
    """A call site that says nothing sends no temperature; pinning is a stated choice."""
    assert LlmRequest.__dataclass_fields__["temperature"].default is None
    assert inspect.signature(g.build_llm_request).parameters["temperature"].default is None


def test_every_call_that_decides_a_compared_field_is_pinned() -> None:
    """rating.citations and the source-of-wealth sources, bands and citations are compared."""
    requests = _dossier_requests()

    assert requests["risk"].temperature == 0.0
    assert requests["sow"].temperature == 0.0


def test_prose_and_the_judge_are_left_free() -> None:
    dossier = _dossier_requests()
    narration = _narration_requests()

    assert dossier["critique"].temperature is None, "a judge of the narrative samples freely"
    assert narration["ubo"].temperature is None
    assert narration["perpetual_kyc"].temperature is None


# --------------------------------------------------------------------------- #
# The managed adapter omits the parameter when the call left it free
# --------------------------------------------------------------------------- #
class _Obj:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


@pytest.fixture
def captured_configs(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """A faked google.genai whose GenerateContentConfig records the kwargs it was built with."""
    seen: list[dict[str, Any]] = []

    class _Config(_Obj):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            seen.append(kwargs)

    types = ModuleType("google.genai.types")
    types.GenerateContentConfig = _Config  # type: ignore[attr-defined]
    types.ThinkingConfig = _Obj  # type: ignore[attr-defined]
    types.Content = _Obj  # type: ignore[attr-defined]
    types.Part = SimpleNamespace(from_text=lambda text: _Obj(text=text))  # type: ignore[attr-defined]
    types.ThinkingLevel = SimpleNamespace(LOW="LOW", HIGH="HIGH")  # type: ignore[attr-defined]
    genai = ModuleType("google.genai")
    genai.types = types  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "google.genai", genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", types)
    return seen


def _call(temperature: float | None) -> None:
    adapter = GeminiLLMAdapter(Settings(profile="gcp"))
    adapter._client = SimpleNamespace(  # type: ignore[assignment]
        models=SimpleNamespace(
            generate_content=lambda **_: SimpleNamespace(text="{}", usage_metadata=None)
        )
    )
    adapter.generate(
        LlmRequest(messages=(LlmMessage(role="user", content="q"),), temperature=temperature)
    )


def test_the_gemini_adapter_sends_no_temperature_for_a_free_call(
    captured_configs: list[dict[str, Any]],
) -> None:
    _call(None)

    assert "temperature" not in captured_configs[-1]


def test_the_gemini_adapter_sends_the_pin_for_a_pinned_call(
    captured_configs: list[dict[str, Any]],
) -> None:
    _call(0.0)

    assert captured_configs[-1]["temperature"] == 0.0
