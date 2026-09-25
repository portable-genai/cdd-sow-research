"""The service half of the model pills: which model ANSWERED, and whether it searched.

The console shows two pills at the top right: the model that answered the last request, and
``Search`` when that answer used an online search tool (owner decision, 2026-09-23). Both come
from response headers the kit emits (``install_answer_provenance`` in ``api/app.py``) for
whatever the model adapters NOTED as they called. Before a request is answered the pill shows
``generator_model`` from ``/v1/healthz``, so that value must be the model the bound adapter
calls, never one a configuration flag names while the adapter calls another.

This repository is the one whose Gemini adapter HONOURS ``models.use_hard_reasoning``, so the
flag stays, and the test that matters is that flipping it moves the adapter's model and
``generator_model`` together.

Every managed adapter here is driven through a faked ``google.genai`` module: the gate runs with
no cloud SDK installed, and a test that needed one would be proving nothing offline.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from hex_service_kit import provenance
from tests.conftest import (
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

from cdd_sow_research.adapters.gcp.gemini_adverse_media import GeminiAdverseMediaAdapter
from cdd_sow_research.adapters.gcp.gemini_llm import GeminiLLMAdapter
from cdd_sow_research.adapters.gcp.ownership_graph import GroundedOwnershipGraphAdapter
from cdd_sow_research.adapters.gcp.registry_lookup import GroundedRegistryAdapter
from cdd_sow_research.adapters.local.llm import LocalDeterministicLLMAdapter
from cdd_sow_research.api import deps
from cdd_sow_research.api.app import _generator_model, app
from cdd_sow_research.config import Settings
from cdd_sow_research.domain.models import LlmMessage, LlmRequest
from cdd_sow_research.domain.services import CddService

ANSWERED_BY = "x-answered-by"
SEARCH_USED = "x-search-used"

_CDD_BODY = {
    "subject": {
        "id": "subj-acme",
        "name": "Acme Holdings Pte Ltd (FICTIONAL)",
        "type": "entity",
        "jurisdiction": "SG",
    },
    "documents": [{"id": "doc-1", "doc_type": "registry_extract", "acl_tags": ["case:subj-acme"]}],
    "actor": "analyst@bank.test",
}


# --------------------------------------------------------------------------- #
# A stand-in for google.genai: records what each call was configured with.
# --------------------------------------------------------------------------- #
class _Obj:
    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)


class _FakeModels:
    def __init__(self, text: str, fail: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self._text = text
        self._fail = fail

    def generate_content(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._fail:
            raise RuntimeError("the model is unavailable (FICTIONAL)")
        return SimpleNamespace(text=self._text, usage_metadata=None)


@pytest.fixture
def fake_genai(monkeypatch: pytest.MonkeyPatch) -> None:
    types = ModuleType("google.genai.types")
    for name in ("Content", "GenerateContentConfig", "Tool", "GoogleSearch", "ThinkingConfig"):
        setattr(types, name, _Obj)
    types.Part = SimpleNamespace(from_text=lambda text: _Obj(text=text))  # type: ignore[attr-defined]
    types.ThinkingLevel = SimpleNamespace(LOW="LOW", HIGH="HIGH")  # type: ignore[attr-defined]
    genai = ModuleType("google.genai")
    genai.types = types  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "google.genai", genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", types)


def _with_client(adapter: Any, models: _FakeModels) -> Any:
    adapter._client = SimpleNamespace(models=models)
    return adapter


def _gcp(**model_overrides: Any) -> Settings:
    settings = Settings(profile="gcp", grounding_enabled=True)
    return replace(settings, models=replace(settings.models, **model_overrides))


def _request(temperature: float | None = None) -> LlmRequest:
    return LlmRequest(messages=(LlmMessage(role="user", content="q"),), temperature=temperature)


def _client_for(cdd: CddService) -> TestClient:
    app.dependency_overrides[deps.get_cdd_service] = lambda: cdd
    return TestClient(app, client=("127.0.0.1", 50000))


def _cdd(**overrides: Any) -> CddService:
    ports: dict[str, Any] = {
        "extraction": FakeExtraction(),
        "knowledge_base": FakeKnowledgeBase(),
        "adverse_media": None,
        "registry": FakeRegistry(),
        "compliance": FakeCompliance(),
        "llm": FakeLLM(),
        "guardrail": FakeGuardrail(),
        "redaction": FakeRedaction(),
        "tracer": FakeTracer(),
        "audit": FakeAudit(),
    }
    ports.update(overrides)
    if ports["adverse_media"] is None:
        from tests.conftest import FakeAdverseMedia

        ports["adverse_media"] = FakeAdverseMedia()
    return CddService(**ports)


@pytest.fixture(autouse=True)
def _clear_overrides() -> Any:
    yield
    app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# The route: the header names what the bound adapter noted
# --------------------------------------------------------------------------- #
def test_a_dossier_under_local_names_the_offline_stub_that_answered() -> None:
    response = _client_for(_cdd()).post("/v1/cdd", json=_CDD_BODY)

    assert response.status_code == 200, response.text
    assert response.headers[ANSWERED_BY] == LocalDeterministicLLMAdapter.STUB_MODEL
    # The same string the pill shows before any answer, so a local run never changes its claim.
    assert response.headers[ANSWERED_BY] == _generator_model(Settings(profile="local"))
    assert SEARCH_USED not in response.headers, "nothing searched, so the pill must not say so"


def test_a_request_that_called_no_model_names_none() -> None:
    """Nothing noted, nothing sent: the pill never invents a model."""
    response = TestClient(app, client=("127.0.0.1", 50000)).get(
        "/v1/healthz", headers={"X-Dev-Persona": "analyst"}
    )

    assert response.status_code == 200
    assert ANSWERED_BY not in response.headers
    assert SEARCH_USED not in response.headers


def test_a_dossier_whose_adverse_media_screen_searched_says_so(fake_genai: None) -> None:
    """The real grounded adapter, on a faked SDK, marks the answer as searched."""
    settings = _gcp(triage="gemini-triage-test")
    media = _with_client(GeminiAdverseMediaAdapter(settings), _FakeModels('{"findings": []}'))

    response = _client_for(_cdd(adverse_media=media)).post("/v1/cdd", json=_CDD_BODY)

    assert response.status_code == 200, response.text
    assert response.headers[SEARCH_USED] == "true"
    answered = [m.strip() for m in response.headers[ANSWERED_BY].split(",")]
    assert "gemini-triage-test" in answered
    assert LocalDeterministicLLMAdapter.STUB_MODEL in answered
    # The next request is a fresh record: a search never leaks into a later answer.
    later = _client_for(_cdd()).post("/v1/cdd", json=_CDD_BODY)
    assert SEARCH_USED not in later.headers


# --------------------------------------------------------------------------- #
# The adapters: each notes the model it CALLED, and search only when the tool was attached
# --------------------------------------------------------------------------- #
def test_the_gemini_adapter_notes_the_model_it_called(fake_genai: None) -> None:
    settings = _gcp(reasoning="gemini-reasoning-test", triage="gemini-triage-test")
    models = _FakeModels('{"a": 1}')
    adapter = _with_client(GeminiLLMAdapter(settings), models)

    with provenance.scope() as record:
        adapter.generate(_request())
        adapter.classify("text", ["x", "y"])

    assert [call["model"] for call in models.calls] == [
        "gemini-reasoning-test",
        "gemini-triage-test",
    ]
    assert record.models == ["gemini-reasoning-test", "gemini-triage-test"]
    assert record.search_used is False, "no search tool was attached to either call"


def test_a_failed_call_notes_nothing(fake_genai: None) -> None:
    adapter = _with_client(GeminiLLMAdapter(_gcp()), _FakeModels("", fail=True))

    with provenance.scope() as record, pytest.raises(RuntimeError):
        adapter.generate(_request())

    assert record.models == []


@pytest.mark.parametrize(
    ("adapter_type", "call"),
    [
        (GroundedRegistryAdapter, lambda a: a.lookup("Acme Holdings (FICTIONAL)", "SG")),
        (GroundedOwnershipGraphAdapter, lambda a: a.hop("Acme Holdings (FICTIONAL)", "SG")),
        (GeminiAdverseMediaAdapter, lambda a: a.search("Acme Holdings (FICTIONAL)")),
    ],
)
def test_every_grounded_research_call_notes_search(
    fake_genai: None, adapter_type: Any, call: Any
) -> None:
    models = _FakeModels("{}")
    adapter = _with_client(adapter_type(_gcp()), models)

    with provenance.scope() as record:
        call(adapter)

    (only,) = models.calls
    assert only["config"].tools, "the call this test pins must actually attach the search tool"
    assert record.search_used is True
    assert record.models == [only["model"]]


def test_a_disabled_adverse_media_screen_searched_nothing(fake_genai: None) -> None:
    settings = replace(_gcp(), grounding_enabled=False)
    models = _FakeModels("{}")
    adapter = _with_client(GeminiAdverseMediaAdapter(settings), models)

    with provenance.scope() as record:
        assert adapter.search("Acme Holdings (FICTIONAL)") is None

    assert models.calls == []
    assert record.search_used is False
    assert record.models == []


# --------------------------------------------------------------------------- #
# generator_model is the model the adapter calls, hard-reasoning opt-in included
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("use_hard_reasoning", [False, True])
def test_flipping_hard_reasoning_moves_the_adapter_and_the_pill_together(
    fake_genai: None, use_hard_reasoning: bool
) -> None:
    """The latent false banner, held shut: one flag, one resolver, both readers.

    Elsewhere in the fleet the flag moved ``generator_model`` while the adapter kept calling
    ``models.reasoning``, so the pill would have named a model that never answered. Here the
    adapter honours it, and the two must move as one. The two model ids are made DIFFERENT, since
    the shipped settings pin both to the same id and would make this test pass by blindness.
    """
    settings = _gcp(
        reasoning="gemini-reasoning-test",
        hard_reasoning="gemini-hard-reasoning-test",
        use_hard_reasoning=use_hard_reasoning,
    )
    models = _FakeModels("{}")
    adapter = _with_client(GeminiLLMAdapter(settings), models)

    with provenance.scope() as record:
        adapter.generate(_request())

    expected = "gemini-hard-reasoning-test" if use_hard_reasoning else "gemini-reasoning-test"
    assert models.calls[0]["model"] == expected
    assert record.models == [expected]
    assert _generator_model(settings) == expected
    assert _generator_model(replace(settings, profile="live")) == expected
