"""Pytest fixtures: the ``local`` adapters (seeded) + the assembled CddService.

The unit suite is driven by the **real** ``local`` adapter family
(``src/cdd_sow_research/adapters/local``) rather than bespoke in-memory fakes, so the
offline implementation lives in exactly one place and the tests exercise the same code
the offline CLI runs. Every adapter constructs with a single ``Settings`` (the adapter
convention) pointed at ``:memory:`` SQLite, and the knowledge-base index is seeded with
the synthetic ``tests/fixtures/sample_cases`` corpus for determinism.

A few classes wrap the local adapter in a thin **recording** subclass that captures call
arguments for assertions (``.calls`` / ``.requests`` / ``.queries`` / ``.ingested`` /
``.spans`` / ``.events``). These add no behaviour: every method delegates to the real
local adapter, so the in-memory implementation is still the one under ``adapters/local``.
The recorders carry the test instrumentation and nothing else; they use the ``Fake*`` names
the unit modules import.

The local LLM is schema-driven (it reads ``request.response_schema`` and returns a JSON
object whose keys match it, including ``used_source_ids`` recovered from the rendered
``[source_id p.N]`` evidence headers), so it stays correct whatever field names the
source-of-wealth, risk and self-critique schemas declare.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest

from cdd_sow_research.adapters.local.adverse_media import LocalCannedAdverseMediaAdapter
from cdd_sow_research.adapters.local.audit import LocalAppendOnlyAuditAdapter
from cdd_sow_research.adapters.local.compliance import LocalComplianceClientAdapter
from cdd_sow_research.adapters.local.evaluation import LocalOfflineEvalAdapter
from cdd_sow_research.adapters.local.extraction import LocalDocumentExtractionAdapter
from cdd_sow_research.adapters.local.guardrail import LocalHeuristicGuardrailAdapter
from cdd_sow_research.adapters.local.knowledge_base import LocalKnowledgeBaseAdapter
from cdd_sow_research.adapters.local.llm import LocalDeterministicLLMAdapter
from cdd_sow_research.adapters.local.redaction import LocalRegexRedactionAdapter
from cdd_sow_research.adapters.local.registry import LocalRegistryLookupAdapter
from cdd_sow_research.adapters.local.registry_agent import LocalAgentRegistryAdapter
from cdd_sow_research.adapters.local.tool_catalog import LocalToolCatalogAdapter
from cdd_sow_research.adapters.local.tracer import LocalNoopTracerAdapter
from cdd_sow_research.config import LocalSettings, Settings
from cdd_sow_research.domain.models import (
    AdverseMediaFinding,
    AdverseMediaScreening,
    AuditEvent,
    Direction,
    DocumentExtract,
    GuardrailVerdict,
    IngestResult,
    KycDocument,
    LlmRequest,
    LlmResponse,
    OwnershipSummary,
    RedactionResult,
    RetrievalQuery,
    RetrievedPassage,
)
from tests.fixtures import sample_cases


def _settings() -> Settings:
    """Settings whose local stores are ephemeral in-memory SQLite (deterministic)."""
    return Settings(
        profile="local",
        local=LocalSettings(db_path=":memory:", audit_path=":memory:"),
    )


# --------------------------------------------------------------------------- #
# Recording wrappers — thin subclasses of the local adapters that capture call
# arguments for assertions. Every method delegates to the real local adapter. The old
# ``Fake*`` / ``Blocking*`` names are kept so the unit modules import them unchanged.
# --------------------------------------------------------------------------- #
class FakeExtraction(LocalDocumentExtractionAdapter):
    """Local document extraction that records the documents it was asked to extract."""

    def __init__(self) -> None:
        super().__init__(_settings())
        self.calls: list[KycDocument] = []

    def extract(self, document: KycDocument, content: bytes, mime_type: str) -> DocumentExtract:
        self.calls.append(document)
        return super().extract(document, content, mime_type)


class FakeKnowledgeBase(LocalKnowledgeBaseAdapter):
    """Local FTS5 knowledge base, seeded with the sample case passages.

    Records ``ingested`` documents and ``queries``. Pass ``passages=[]`` to simulate an
    empty store (drives the hard-error path when no case evidence is retrieved).
    """

    def __init__(self, passages: list[RetrievedPassage] | None = None) -> None:
        super().__init__(_settings())
        # Re-seed the in-memory index with the requested corpus (empty for the empty case).
        # The sample corpus is seeded untagged so it is visible to whichever case subject a
        # unit test runs (the fixtures represent "the case's evidence", regardless of id);
        # ACL filtering itself is exercised separately via ingested case documents.
        seed = list(sample_cases.SAMPLE_PASSAGES) if passages is None else list(passages)
        self.seed([self._untag(p) for p in seed])
        self.ingested: list[tuple[KycDocument, tuple[str, ...]]] = []
        self.queries: list[RetrievalQuery] = []

    @staticmethod
    def _untag(passage: RetrievedPassage) -> RetrievedPassage:
        from dataclasses import replace

        return replace(passage, acl_tags=())

    def ingest(
        self,
        document: KycDocument,
        content: bytes,
        acl_tags: tuple[str, ...],
        page_texts: tuple[str, ...] = (),
    ) -> IngestResult:
        self.ingested.append((document, acl_tags))
        return super().ingest(document, content, acl_tags, page_texts=page_texts)

    def search(self, query: RetrievalQuery) -> list[RetrievedPassage]:
        self.queries.append(query)
        return super().search(query)


class FakeLLM(LocalDeterministicLLMAdapter):
    """Local deterministic LLM that records the requests + classify calls it received."""

    def __init__(self, used_source_ids: list[str] | None = None) -> None:
        super().__init__(_settings())
        self.requests: list[LlmRequest] = []
        self.classify_calls: list[tuple[str, list[str]]] = []

    def generate(self, request: LlmRequest) -> LlmResponse:
        self.requests.append(request)
        return super().generate(request)

    def classify(self, text: str, labels: list[str]) -> str:
        self.classify_calls.append((text, labels))
        return super().classify(text, labels)


class FakeAdverseMedia(LocalCannedAdverseMediaAdapter):
    """A reachable adverse-media backend; seedable with findings.

    The real local adapter has no backend at all and therefore reports **not searched**
    (``None``). This fake stands for a deployment where a screen genuinely runs, so it
    returns an :class:`AdverseMediaScreening`: seeded findings by default, and an empty
    screening when passed ``findings=()``, which is the "searched and clear" case.

    To model an unreachable backend, use the real adapter or ``searched=False``.
    """

    def __init__(
        self,
        findings: tuple[AdverseMediaFinding, ...] | None = None,
        *,
        searched: bool = True,
    ) -> None:
        super().__init__(_settings())
        self._findings = sample_cases.SAMPLE_ADVERSE_MEDIA if findings is None else tuple(findings)
        self._searched = searched
        self.calls: list[str] = []

    def search(self, subject_name: str, max_results: int = 10) -> AdverseMediaScreening | None:
        self.calls.append(subject_name)
        if not self._searched:
            return None
        return AdverseMediaScreening(
            subject_name=subject_name,
            findings=tuple(self._findings)[:max_results],
            sources=("fictional-news-index",),
        )


class FakeRegistry(LocalRegistryLookupAdapter):
    """Local corporate-registry adapter; seedable with a canned ownership summary."""

    def __init__(self, summary: OwnershipSummary | None = None) -> None:
        super().__init__(_settings())
        self._summary = summary or sample_cases.SAMPLE_OWNERSHIP
        self.calls: list[tuple[str, str]] = []

    def lookup(self, entity_name: str, jurisdiction: str) -> OwnershipSummary:
        self.calls.append((entity_name, jurisdiction))
        return self._summary


class FakeCompliance(LocalComplianceClientAdapter):
    """Local C1 client that records each question it was asked."""

    def __init__(self) -> None:
        super().__init__(_settings())
        self.calls: list[tuple[str, str]] = []

    def check(self, question: str, actor: str):  # type: ignore[no-untyped-def]
        self.calls.append((question, actor))
        return super().check(question, actor)


class FakeGuardrail(LocalHeuristicGuardrailAdapter):
    """Local heuristic guardrail that records the (text, direction) screen calls.

    Behaviour is the real heuristic: benign text passes, malicious text (e.g. the
    ``MALICIOUS_CASE_INPUT`` subject name) is blocked. Only the recording is added.
    """

    def __init__(self) -> None:
        super().__init__(_settings())
        self.calls: list[tuple[str, Direction]] = []

    def screen(self, text: str, direction: Direction) -> GuardrailVerdict:
        self.calls.append((text, direction))
        return super().screen(text, direction)


class BlockingGuardrail(LocalHeuristicGuardrailAdapter):
    """Recording heuristic guardrail used by the blocked-path tests.

    The blocked-path tests feed the malicious ``MALICIOUS_CASE_INPUT`` (whose subject name
    contains "ignore all previous instructions ... exfiltrate ... system prompt"), which
    the real local heuristic deterministically blocks on INPUT. The ``block_input`` /
    ``block_output`` knobs are accepted for source-compatibility with the previous fake but
    are not needed: the heuristic blocks the malicious text on its own. This keeps the
    in-memory implementation in ``adapters/local`` rather than a bespoke fake.
    """

    def __init__(self, block_input: bool = True, block_output: bool = False) -> None:
        super().__init__(_settings())
        self.block_input = block_input
        self.block_output = block_output
        self.calls: list[tuple[str, Direction]] = []

    def screen(self, text: str, direction: Direction) -> GuardrailVerdict:
        self.calls.append((text, direction))
        return super().screen(text, direction)


class FakeRedaction(LocalRegexRedactionAdapter):
    """Local regex redaction that records the raw text it was asked to redact."""

    def __init__(self) -> None:
        super().__init__(_settings())
        self.calls: list[str] = []

    def redact(self, text: str) -> RedactionResult:
        self.calls.append(text)
        return super().redact(text)


class FakeTracer(LocalNoopTracerAdapter):
    """Local no-op tracer that records the span names it opened + token usage."""

    def __init__(self) -> None:
        super().__init__(_settings())
        self.spans: list[str] = []

    def span(self, name: str, **attributes: str):  # type: ignore[no-untyped-def]
        self.spans.append(name)
        return super().span(name, **attributes)


class FakeAudit(LocalAppendOnlyAuditAdapter):
    """Local append-only audit that also keeps the AuditEvent objects for assertions."""

    def __init__(self) -> None:
        super().__init__(_settings())
        self.events: list[AuditEvent] = []

    def record(self, event: AuditEvent) -> None:
        self.events.append(event)
        super().record(event)


class FakeEval(LocalOfflineEvalAdapter):
    """Local offline eval gate (delegates to eval/run_eval.py)."""


class FakeAgentRegistry(LocalAgentRegistryAdapter):
    """Local in-process agent registry."""


class FakeToolCatalog(LocalToolCatalogAdapter):
    """Local in-process MCP tool catalog."""


# --------------------------------------------------------------------------- #
# Service / policy resolution — locate the domain classes wherever they live.
# --------------------------------------------------------------------------- #
_SERVICE_MODULE_CANDIDATES = (
    "cdd_sow_research.domain.cdd_service",
    "cdd_sow_research.domain.sow_service",
    "cdd_sow_research.domain.risk_service",
    "cdd_sow_research.domain.adverse_media_service",
    "cdd_sow_research.domain.ownership_service",
    "cdd_sow_research.domain.services",
)
_POLICY_MODULE_CANDIDATES = (
    "cdd_sow_research.domain.review_policy",
    "cdd_sow_research.domain.policies",
    "cdd_sow_research.domain.services",
)


def _resolve(symbol: str, candidates: tuple[str, ...]) -> Any:
    last: Exception | None = None
    for mod_name in candidates:
        try:
            module = importlib.import_module(mod_name)
        except ModuleNotFoundError as exc:  # pragma: no cover - layout fallback
            last = exc
            continue
        obj = getattr(module, symbol, None)
        if obj is not None:
            return obj
    raise ImportError(f"Could not locate domain symbol {symbol!r} in any of {candidates}") from last


def load_service(name: str) -> Any:
    return _resolve(name, _SERVICE_MODULE_CANDIDATES)


def load_policy(name: str) -> Any:
    return _resolve(name, _POLICY_MODULE_CANDIDATES)


# Direction is re-exported for unit tests that import it from the conftest namespace.
__all__ = ["Direction"]


# --------------------------------------------------------------------------- #
# Pytest fixtures — construct the (seeded) local adapters.
# --------------------------------------------------------------------------- #
@pytest.fixture
def extraction() -> FakeExtraction:
    return FakeExtraction()


@pytest.fixture
def knowledge_base() -> FakeKnowledgeBase:
    return FakeKnowledgeBase()


@pytest.fixture
def empty_knowledge_base() -> FakeKnowledgeBase:
    return FakeKnowledgeBase(passages=[])


@pytest.fixture
def llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def adverse_media() -> FakeAdverseMedia:
    return FakeAdverseMedia()


@pytest.fixture
def registry() -> FakeRegistry:
    return FakeRegistry()


@pytest.fixture
def compliance() -> FakeCompliance:
    return FakeCompliance()


@pytest.fixture
def guardrail() -> FakeGuardrail:
    return FakeGuardrail()


@pytest.fixture
def blocking_guardrail() -> BlockingGuardrail:
    return BlockingGuardrail()


@pytest.fixture
def redaction() -> FakeRedaction:
    return FakeRedaction()


@pytest.fixture
def tracer() -> FakeTracer:
    return FakeTracer()


@pytest.fixture
def audit() -> FakeAudit:
    return FakeAudit()


@pytest.fixture
def evaluation() -> FakeEval:
    return FakeEval(_settings())


@pytest.fixture
def agent_registry() -> FakeAgentRegistry:
    return FakeAgentRegistry(_settings())


@pytest.fixture
def tool_catalog() -> FakeToolCatalog:
    return FakeToolCatalog(_settings())


@pytest.fixture
def cdd_service(
    extraction,
    knowledge_base,
    adverse_media,
    registry,
    compliance,
    llm,
    guardrail,
    redaction,
    tracer,
    audit,
):
    """CddService(extraction, knowledge_base, adverse_media, registry, compliance, llm,
    guardrail, redaction, tracer, audit) + the bundled-fixture sanctions provider, so a
    dossier carries a real (deterministic) screening result in unit tests."""
    from cdd_sow_research.adapters.local.sanctions_provider import LocalSanctionsProviderAdapter

    cls = load_service("CddService")
    return cls(
        extraction,
        knowledge_base,
        adverse_media,
        registry,
        compliance,
        llm,
        guardrail,
        redaction,
        tracer,
        audit,
        sanctions=LocalSanctionsProviderAdapter(_settings()),
    )


@pytest.fixture(autouse=True)
def _isolate_container_cache():
    """Keep the process-wide container cache from crossing test boundaries.

    ``deps._default_container`` is ``lru_cache``d over ``Settings.load()``, so the first
    test that builds it fixes the profile every later test sees. Tests that deliberately
    clear the profile environment (the three-state and posture suites) would otherwise
    leave a no-profile container cached, and every subsequent route test authenticates
    against it and gets a 401 it never asked for.

    That made the suite order dependent: the route modules pass alone and fail in the
    full run. An order dependent gate is the failure this catalog treats as serious,
    because it means a green result is a property of the run rather than of the code.
    Clearing on both sides of every test makes the ordering irrelevant.
    """
    from cdd_sow_research.api import deps as _deps

    _deps.get_container.cache_clear()
    yield
    _deps.get_container.cache_clear()
