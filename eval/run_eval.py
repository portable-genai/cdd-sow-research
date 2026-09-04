#!/usr/bin/env python3
"""Evaluation for the B1 CDD + Source-of-Wealth Agent — A4 / P-08.

Two named layers (--mode):

* **smoke** (default) — the offline pre-merge check CI runs on every change; the build
  fails if the agent's dossiers fall below the model-risk thresholds agreed for a
  regulated financial-crime agent (see ``eval/rubrics/*.yaml``). It is a smoke check, NOT
  the promotion authority.
* **gate** — the promotion verdict from the shared model-quality-gate AI-quality service via the
  ``EvaluationGatePort`` (requires ``CDD_PROFILE=platform|gcp``); it fails closed on the
  reconciled evaluate + gate result.

The smoke thresholds::

    sow_groundedness   >= 0.80   (every narrative claim is cited)
    risk_band_accuracy >= 0.80   (assigned band matches the expected band)
    citation_accuracy  >= 0.90   (cites only retrieved/derived sources)
    pii_safety         >= 0.99   (no unredacted PII leaks into the dossier or audit)
    pkyc_priority      >= 0.90   (pKYC queue place + re-score direction match the golden set)
    ubo_accuracy       >= 0.90   (UBO owners, effective %, control basis + flags match it)

The smoke evaluator is a deterministic, dependency-light heuristic in this file: it needs
**no GCP credentials and no Google Cloud SDK**, runs the real ``CddService`` pipeline
against in-memory fake adapters, and computes the four metrics with conservative
set/string heuristics. ``--mode gate`` instead routes through ``EvaluationGatePort`` to
the model-quality-gate authority (the richer judged check run pre-promotion).
# verify: https://docs.cloud.google.com/vertex-ai/generative-ai/docs/models/run-evaluation

Usage::

    python eval/run_eval.py                      # offline smoke check (CI, default)
    python eval/run_eval.py --dataset path.jsonl # custom golden set
    python eval/run_eval.py --mode gate          # promotion verdict via model-quality-gate
    (platform/gcp)

Exit code is ``0`` iff ``EvalReport.passed`` (every metric meets its threshold).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Any

# The --mode smoke|gate scaffold + the aligned report rendering come from the shared agent-eval-kit
# commons; this script keeps only its own offline evaluator and gate runner.
from agent_eval_kit import eval_main

# Domain models are pure-stdlib (no GCP / framework imports), so importing them here keeps
# this script runnable in the on-prem/test profile with no Google Cloud SDK installed.
from cdd_sow_research.domain.models import (
    AdverseMediaCategory,
    AdverseMediaFinding,
    AdverseMediaScreening,
    BeneficialOwner,
    CaseInput,
    CDDCase,
    Citation,
    Direction,
    DocumentExtract,
    EvalMetricResult,
    EvalReport,
    GuardrailVerdict,
    IngestResult,
    KycDocument,
    ListSource,
    LlmRequest,
    LlmResponse,
    OwnershipEdge,
    OwnershipEdgeKind,
    OwnershipGraphNode,
    OwnershipNode,
    OwnershipNodeKind,
    OwnershipSummary,
    RedactionResult,
    RegistryHop,
    RetrievalQuery,
    RetrievedPassage,
    ScreeningAlert,
    ScreeningMatch,
    ScreeningResult,
    Severity,
    SourceType,
    Subject,
    SubjectType,
    TokenUsage,
    WatchlistEntry,
)
from cdd_sow_research.domain.perpetual_kyc import PerpetualKycEngine
from cdd_sow_research.domain.policy import CountryRiskPolicy, PerpetualKycPolicy, UboGraphPolicy
from cdd_sow_research.domain.ubo_graph import UboGraphEngine, ownership_node_id

THRESHOLDS: dict[str, float] = {
    "sow_groundedness": 0.80,
    "risk_band_accuracy": 0.80,
    "citation_accuracy": 0.90,
    "pii_safety": 0.99,
    # Perpetual KYC: does the engine put a changed relationship in the queue place the
    # golden set INDEPENDENTLY says it belongs in? The oracle is the dataset's own
    # ``perpetual_kyc.expected_priority``, never a re-read of what the engine produced,
    # so a broken engine scores red instead of agreeing with itself.
    "pkyc_priority": 0.90,
    # UBO graph: replay the registry layers the golden case DECLARES through the real
    # engine, and compare the beneficial owners, their effective percentages, the control
    # basis and the indicators against what the same case declares they must be. Same
    # discipline: the oracle is the dataset, never the pipeline's own output.
    "ubo_accuracy": 0.90,
}

_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATASET = _REPO_ROOT / "eval" / "datasets" / "golden_cases.jsonl"

# PII patterns are jurisdiction-driven (compliance packs): a non-SG fork sets
# CDD_PII_JURISDICTIONS (comma-separated ISO-3166 codes) so its gate detects its own
# national identifiers (PAN, Aadhaar, NINO, NIK, ...) instead of being falsely green on
# the strictest metric. FakeRedaction masks and score_pii_safety detects with the SAME
# pattern set, so leakage means the pipeline re-introduced PII that bypassed redaction.
from cdd_sow_research.domain import pii_patterns  # noqa: E402
from cdd_sow_research.envread import read_env_setting  # noqa: E402


def _pii_jurisdictions(raw: str | None) -> tuple[str, ...]:
    """Three-state read of ``CDD_PII_JURISDICTIONS``; an empty override REFUSES.

    Unset means "no override", so the gate scores against the shipped default pack. Set to
    a value that names no jurisdiction (``""``, ``","``, whitespace) is not a request for
    fewer detectors, it is a broken override: honouring it would leave the gate scoring PII
    safety with the national-identifier patterns switched off and reporting green while a
    national id leaked. Only a value that names at least one jurisdiction narrows the set.
    """
    if raw is None:
        return tuple(pii_patterns.DEFAULT_JURISDICTIONS)
    codes = tuple(code.strip().upper() for code in raw.split(",") if code.strip())
    if not codes:
        raise SystemExit(
            "CDD_PII_JURISDICTIONS is set but names no jurisdiction; refusing to score PII "
            "safety with an empty detector set. Unset it to use the default pack "
            f"({','.join(pii_patterns.DEFAULT_JURISDICTIONS)}), or name the codes to detect."
        )
    return codes


_PII_JURISDICTIONS = _pii_jurisdictions(read_env_setting("CDD_PII_JURISDICTIONS").raw)
_PII_PATTERNS = pii_patterns.patterns_for(_PII_JURISDICTIONS)


# --------------------------------------------------------------------------- #
# Golden dataset
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class GoldenExample:
    id: str
    subject_name: str
    subject_type: str
    jurisdiction: str
    documents: tuple[str, ...]
    evidence: tuple[str, ...]  # passage texts (each becomes a cited DOCUMENT passage)
    expected_wealth_sources: tuple[str, ...]
    expected_risk_band: str
    expected_adverse_categories: tuple[str, ...]
    pep_owner: bool = False
    pii_in_inputs: bool = False
    # Perpetual-KYC scenario: the change to simulate against the established baseline,
    # and the queue priority the golden set says that change must produce.
    pkyc_change: str = "no_change"
    expected_pkyc_priority: str = "low"
    expected_pkyc_direction: str = "flat"  # up | flat | down
    # UBO-graph scenario: the registry layers to replay, and the answer the golden set
    # INDEPENDENTLY says those layers must produce.
    ubo: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ubo_chain(self) -> str:
        return str(self.ubo.get("chain_type", "none"))


def load_golden(path: Path) -> list[GoldenExample]:
    examples: list[GoldenExample] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:  # pragma: no cover - defensive
            raise SystemExit(f"{path}:{lineno}: invalid JSON: {exc}") from exc
        examples.append(
            GoldenExample(
                id=str(obj.get("id", f"example-{lineno}")),
                subject_name=str(obj["subject_name"]),
                subject_type=str(obj.get("subject_type", "entity")),
                jurisdiction=str(obj.get("jurisdiction", "")),
                documents=tuple(obj.get("documents", []) or ()),
                evidence=tuple(obj.get("evidence", []) or ()),
                expected_wealth_sources=tuple(obj.get("expected_wealth_sources", []) or ()),
                expected_risk_band=str(obj.get("expected_risk_band", "medium")),
                expected_adverse_categories=tuple(obj.get("expected_adverse_categories", []) or ()),
                pep_owner=bool(obj.get("pep_owner", False)),
                pii_in_inputs=bool(obj.get("pii_in_inputs", False)),
                pkyc_change=str((obj.get("perpetual_kyc") or {}).get("change", "no_change")),
                expected_pkyc_priority=str(
                    (obj.get("perpetual_kyc") or {}).get("expected_priority", "low")
                ),
                expected_pkyc_direction=str(
                    (obj.get("perpetual_kyc") or {}).get("expected_delta_direction", "flat")
                ),
                ubo=dict(obj.get("ubo") or {}),
            )
        )
    if not examples:
        raise SystemExit(f"{path}: golden dataset is empty")
    return examples


def load_thresholds_from_rubrics() -> dict[str, float]:
    """Read thresholds from ``eval/rubrics/*.yaml`` when PyYAML is available."""
    thresholds = dict(THRESHOLDS)
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        return thresholds
    rubric_dir = _REPO_ROOT / "eval" / "rubrics"
    for name in (
        "sow_groundedness.yaml",
        "citation_accuracy.yaml",
        "pkyc_priority.yaml",
        "ubo_accuracy.yaml",
    ):
        rubric_path = rubric_dir / name
        if not rubric_path.exists():
            continue
        doc = yaml.safe_load(rubric_path.read_text(encoding="utf-8")) or {}
        metric = doc.get("metric")
        if isinstance(metric, str) and "threshold" in doc:
            thresholds[metric] = float(doc["threshold"])
        for companion, spec in (doc.get("companion_metrics") or {}).items():
            if isinstance(spec, dict) and "threshold" in spec:
                thresholds[str(companion)] = float(spec["threshold"])
    return thresholds


# --------------------------------------------------------------------------- #
# Deterministic fake adapters (inlined: importing tests.conftest is disallowed for the
# gate, and CI must not depend on the test tree). Together they let the real CddService
# assessment pipeline run end-to-end with zero external services.
# --------------------------------------------------------------------------- #
class FakeRedaction:
    """Masks NRIC + email like DLP, so the gate can prove no PII survives (pii_safety)."""

    def redact(self, text: str) -> RedactionResult:
        redacted = text
        for info_type, pattern in _PII_PATTERNS:
            redacted = pattern.sub(f"[{info_type}]", redacted)
        return RedactionResult(text=redacted, findings=())


class FakeGuardrail:
    def screen(self, text: str, direction: Direction) -> GuardrailVerdict:
        return GuardrailVerdict(
            allowed=True, direction=direction, findings=(), sanitized_text=text, reason="benign"
        )


class FakeExtraction:
    def extract(self, document: KycDocument, content: bytes, mime_type: str) -> DocumentExtract:
        return DocumentExtract(document_id=document.id, text="", pages=1)


class FakeKnowledgeBase:
    def __init__(self, by_subject: dict[str, GoldenExample]) -> None:
        self._by_subject = by_subject

    def ingest(self, document, content, acl_tags) -> IngestResult:  # type: ignore[no-untyped-def]
        return IngestResult(document_id=document.id, chunks=1, status="indexed", ok=True)

    def search(self, query: RetrievalQuery) -> list[RetrievedPassage]:
        example = self._lookup(query)
        passages: list[RetrievedPassage] = []
        for i, text in enumerate(example.evidence if example else ()):
            citation = Citation(
                source_id=f"doc-{example.id}-{i}",
                source_type=SourceType.DOCUMENT,
                title=f"Evidence {i} for {example.subject_name}",
                page=i + 1,
                snippet=text[:120],
                score=round(0.95 - i * 0.05, 3),
            )
            passages.append(
                RetrievedPassage(text=text, citation=citation, score=citation.score or 0)
            )
        return passages

    def _lookup(self, query: RetrievalQuery) -> GoldenExample | None:
        for example in self._by_subject.values():
            if example.subject_name in query.text:
                return example
        return None


class FakeAdverseMedia:
    def __init__(self, by_subject: dict[str, GoldenExample]) -> None:
        self._by_subject = by_subject

    def search(self, subject_name: str, max_results: int = 10) -> AdverseMediaScreening | None:
        example = self._by_subject.get(subject_name)
        if example is None:
            # A subject outside the golden set was never screened here, and the eval must
            # not score it as though a backend had cleared it.
            return None
        findings: list[AdverseMediaFinding] = []
        for cat in example.expected_adverse_categories:
            category = _CATEGORY_BY_VALUE.get(cat, AdverseMediaCategory.OTHER)
            findings.append(
                AdverseMediaFinding(
                    headline=f"{cat} concern for {subject_name} (FICTIONAL)",
                    publisher="Example Wire",
                    url=f"https://example.test/{cat}",
                    category=category,
                    severity=Severity.HIGH,
                )
            )
        return AdverseMediaScreening(
            subject_name=subject_name,
            findings=tuple(findings[:max_results]),
            sources=("fictional-news-index",),
        )


class FakeRegistry:
    def __init__(self, by_subject: dict[str, GoldenExample]) -> None:
        self._by_subject = by_subject

    def lookup(self, entity_name: str, jurisdiction: str) -> OwnershipSummary:
        example = self._by_subject.get(entity_name)
        pep = bool(example and example.pep_owner)
        return OwnershipSummary(
            root_entity=entity_name,
            owners=(
                BeneficialOwner(name="UBO (FICTIONAL)", pct=75.0, country=jurisdiction, is_pep=pep),
            ),
            tree=OwnershipNode(entity_name=entity_name, pct=100.0),
        )


class FakeCompliance:
    def check(self, question: str, actor: str):  # type: ignore[no-untyped-def]
        from cdd_sow_research.ports.compliance import ComplianceAnswer

        return ComplianceAnswer(question=question, answer="CDD/AML expectations apply.")


class FakeTracer:
    @contextmanager
    def span(self, name: str, **attributes: str) -> Iterator[None]:
        yield

    def record_token_usage(self, usage: TokenUsage, model: str) -> None:
        return None


class FakeAudit:
    def __init__(self) -> None:
        self.events: list[object] = []

    def record(self, event: object) -> None:
        self.events.append(event)


_CATEGORY_BY_VALUE = {c.value: c for c in AdverseMediaCategory}
_WEALTH_KINDS = (
    "employment",
    "business_ownership",
    "inheritance",
    "investments",
    "asset_sale",
    "other",
)

_EVIDENCE_QUESTION_RE = re.compile(r"\[([a-z0-9][a-z0-9\-]*)(?:\s+p\.[^\]]+)?\]")


class FakeLLM:
    """Deterministic, grounded synthesis keyed off the case evidence headers.

    The real ``CddService`` calls ``generate`` with a structured-output request whose user
    content carries the EVIDENCE block of ``[source_id p.N] (...)`` headers. This fake
    plays the model honestly: it cites only the source_ids actually present in EVIDENCE,
    and shapes the source-of-wealth narrative from the example's expected wealth sources.
    """

    def __init__(self, by_subject: dict[str, GoldenExample]) -> None:
        self._by_subject = by_subject
        self.model = "gemini-3.5-flash"

    def generate(self, request: LlmRequest) -> LlmResponse:
        user = request.messages[-1].content if request.messages else ""
        source_ids = self._source_ids(user)
        example = self._example_for(user)
        schema = request.response_schema or {}
        props = (schema.get("properties") or {}) if isinstance(schema, dict) else {}
        if "sources" in props:
            payload = self._sow_payload(example, source_ids)
        elif "factors" in props:
            payload = self._risk_payload(example, source_ids)
        else:
            payload = {"grounded": True, "confidence": 0.9, "caveats": []}
        return LlmResponse(
            text=json.dumps(payload),
            usage=TokenUsage(input_tokens=128, output_tokens=64, thinking_tokens=16),
            model=self.model,
        )

    def classify(self, text: str, labels: list[str]) -> str:
        return labels[0] if labels else ""

    def _sow_payload(self, example: GoldenExample | None, source_ids: list[str]) -> dict:
        kinds = list(example.expected_wealth_sources) if example else ["business_ownership"]
        sources = [
            {
                "kind": k if k in _WEALTH_KINDS else "other",
                "description": f"Wealth from {k.replace('_', ' ')}.",
                "est_value_band": "USD 1m-5m",
                "used_source_ids": source_ids,
            }
            for k in kinds
        ]
        narrative = " ".join(f"The subject's wealth includes {k.replace('_', ' ')}." for k in kinds)
        return {
            "narrative": narrative,
            "sources": sources,
            "confidence": 0.9 if source_ids else 0.2,
            "used_source_ids": source_ids,
        }

    def _risk_payload(self, example: GoldenExample | None, source_ids: list[str]) -> dict:
        band = example.expected_risk_band if example else "medium"
        # The model emits a base band; the service deterministically raises it on hard
        # signals (sanctions/terrorism/PEP), so for those cases we emit a lower base band
        # to prove the raise actually happens.
        base = "low" if band in ("high", "prohibited") else band
        return {
            "band": base,
            "score": 0.5,
            "factors": [
                {
                    "name": "source_of_wealth_transparency",
                    "weight": 0.4,
                    "present": True,
                    "detail": "Wealth is documented in the case evidence.",
                    "used_source_ids": source_ids,
                }
            ],
            "rationale": "Risk assessed from the case evidence.",
            "used_source_ids": source_ids,
        }

    def _example_for(self, user: str) -> GoldenExample | None:
        for example in self._by_subject.values():
            if example.subject_name in user:
                return example
        return None

    @staticmethod
    def _source_ids(user: str) -> list[str]:
        seen: list[str] = []
        for sid in _EVIDENCE_QUESTION_RE.findall(user):
            if sid not in seen:
                seen.append(sid)
        return seen


# --------------------------------------------------------------------------- #
# Pipeline driver
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class _Adapters:
    extraction: FakeExtraction
    knowledge_base: FakeKnowledgeBase
    adverse_media: FakeAdverseMedia
    registry: FakeRegistry
    compliance: FakeCompliance
    llm: FakeLLM
    guardrail: FakeGuardrail
    redaction: FakeRedaction
    tracer: FakeTracer
    audit: FakeAudit


def _build_adapters(examples: list[GoldenExample]) -> _Adapters:
    by_subject = {ex.subject_name: ex for ex in examples}
    return _Adapters(
        extraction=FakeExtraction(),
        knowledge_base=FakeKnowledgeBase(by_subject),
        adverse_media=FakeAdverseMedia(by_subject),
        registry=FakeRegistry(by_subject),
        compliance=FakeCompliance(),
        llm=FakeLLM(by_subject),
        guardrail=FakeGuardrail(),
        redaction=FakeRedaction(),
        tracer=FakeTracer(),
        audit=FakeAudit(),
    )


def _make_service(adapters: _Adapters):  # type: ignore[no-untyped-def]
    from cdd_sow_research.domain.cdd_service import CddService

    return CddService(
        extraction=adapters.extraction,
        knowledge_base=adapters.knowledge_base,
        adverse_media=adapters.adverse_media,
        registry=adapters.registry,
        compliance=adapters.compliance,
        llm=adapters.llm,
        guardrail=adapters.guardrail,
        redaction=adapters.redaction,
        tracer=adapters.tracer,
        audit=adapters.audit,
    )


def _case_input(example: GoldenExample) -> CaseInput:
    name = example.subject_name
    if example.pii_in_inputs:
        # Inject obviously-fake PII into the name to prove redaction (pii_safety).
        name = f"{name}, NRIC S1234567A, contact ops@example.com"
    subject = Subject(
        id=example.id,
        name=name,
        type=SubjectType(example.subject_type)
        if example.subject_type in ("individual", "entity")
        else SubjectType.ENTITY,
        jurisdiction=example.jurisdiction,
    )
    documents = tuple(
        KycDocument(id=f"{example.id}-{d}", acl_tags=(f"case:{example.id}",))
        for d in example.documents
    )
    return CaseInput(subject=subject, documents=documents)


# --------------------------------------------------------------------------- #
# Heuristic scorers
# --------------------------------------------------------------------------- #
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _claim_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_SPLIT.split(text or "") if len(s.strip()) >= 12]


def score_sow_groundedness(case: CDDCase) -> float:
    """Every narrative claim must be backed by at least one citation."""
    sentences = _claim_sentences(case.sow.narrative)
    if not sentences:
        return 1.0
    return 1.0 if case.sow.citations else 0.0


def score_citation_accuracy(case: CDDCase, retrieved_ids: set[str]) -> float:
    """No cited source outside the retrieved/derived set (fabrication check)."""
    cited = {c.source_id for c in case.sow.citations}
    cited |= {c.source_id for c in case.rating.citations}
    if not cited:
        return 0.0 if _claim_sentences(case.sow.narrative) else 1.0
    allowed = set(retrieved_ids)
    for f in case.adverse_media.findings if case.adverse_media is not None else ():
        if f.citation is not None:
            allowed.add(f.citation.source_id)
    if case.ownership is not None:
        allowed.update(c.source_id for o in case.ownership.owners for c in o.citations)
        allowed.update(c.source_id for c in case.ownership.citations)
    return round(len(cited & allowed) / len(cited), 4)


def score_risk_band_accuracy(case: CDDCase, expected_band: str) -> float:
    return 1.0 if case.rating.band.value == expected_band else 0.0


def score_pii_safety(case: CDDCase, audit_events: list[object]) -> float:
    """1.0 unless any unredacted NRIC/email survived into the dossier or audit."""
    haystacks = [case.sow.narrative, case.rating.rationale]
    haystacks.extend(s.description for s in case.sow.sources)
    for event in audit_events:
        haystacks.append(str(getattr(event, "redacted_prompt", "")))
        haystacks.append(str(getattr(event, "redacted_response", "")))
    return (
        0.0
        if any(pattern.search(h or "") for h in haystacks for _, pattern in _PII_PATTERNS)
        else 1.0
    )


# --------------------------------------------------------------------------- #
# Perpetual-KYC scorer
#
# Independent oracle: each golden case declares the CHANGE to simulate and the queue
# priority that change must produce (``perpetual_kyc.expected_priority``). The scorer
# replays that change through the real engine and compares the computed priority to the
# dataset's expectation. It never reads the engine's own opinion of itself, so a broken
# or mis-tuned engine scores red rather than agreeing with itself.
# --------------------------------------------------------------------------- #
_PKYC_AS_OF = date(2026, 8, 5)
_PKYC_CHANGED_AT = date(2026, 9, 1)


def _pkyc_subject(example: GoldenExample) -> Subject:
    return Subject(
        id=example.id,
        name=example.subject_name,
        type=SubjectType(example.subject_type)
        if example.subject_type in ("individual", "entity")
        else SubjectType.ENTITY,
        jurisdiction=example.jurisdiction,
        tenant="golden-bank",
    )


def _pkyc_baseline_ownership(example: GoldenExample) -> OwnershipSummary:
    """The ownership picture at the baseline (fictional, dataset-independent)."""
    return OwnershipSummary(
        root_entity=example.subject_name,
        owners=(
            BeneficialOwner(
                name=f"Baseline Owner of {example.subject_name}",
                pct=75.0,
                country=example.jurisdiction,
                is_pep=example.pep_owner,
            ),
        ),
    )


def _pkyc_new_screening(example: GoldenExample) -> ScreeningResult:
    entry = WatchlistEntry(
        uid=f"FICTIONAL-{example.id}",
        source=ListSource.OFAC_SDN,
        name=f"Invented Designated Party for {example.id} (FICTIONAL)",
        list_version="2026-09-01",
    )
    match = ScreeningMatch(
        entry=entry, score=0.95, matched_name=entry.name, features=("name 0.95",)
    )
    return ScreeningResult(
        subject_id=example.id,
        query_name=example.subject_name,
        lists_version="2026-09-01",
        sources=(ListSource.OFAC_SDN,),
        alerts=(ScreeningAlert(id=f"alert-{example.id}", subject_id=example.id, match=match),),
    )


def _pkyc_new_media(example: GoldenExample) -> AdverseMediaFinding:
    return AdverseMediaFinding(
        headline=f"Fictional negative-news item concerning {example.subject_name}",
        publisher="The Invented Times (FICTIONAL)",
        url=f"https://example.test/{example.id}/fictional-update",
        category=AdverseMediaCategory.OTHER,
        severity=Severity.HIGH,
        snippet="Entirely fictional reporting used only as a golden-set fixture.",
    )


def computed_pkyc_outcome(engine: Any, example: GoldenExample) -> tuple[str, str]:
    """Replay the declared change through ``engine``: (queue priority, score direction)."""
    subject = _pkyc_subject(example)
    baseline_ownership = _pkyc_baseline_ownership(example)
    first = engine.assess(subject=subject, as_of=_PKYC_AS_OF, ownership=baseline_ownership)
    baseline = engine.next_baseline(first)

    kwargs: dict[str, Any] = {"ownership": baseline_ownership}
    if example.pkyc_change == "new_sanctions_hit":
        kwargs["screening"] = _pkyc_new_screening(example)
    elif example.pkyc_change == "new_adverse_media":
        kwargs["adverse_media"] = (_pkyc_new_media(example),)
    elif example.pkyc_change == "ownership_change":
        kwargs["ownership"] = OwnershipSummary(
            root_entity=baseline_ownership.root_entity,
            owners=(replace(baseline_ownership.owners[0], pct=51.0),),
        )

    assessment = engine.assess(subject=subject, as_of=_PKYC_CHANGED_AT, baseline=baseline, **kwargs)
    priority = assessment.queue_item.priority.value if assessment.queue_item else ""
    if assessment.score_delta > 0:
        direction = "up"
    elif assessment.score_delta < 0:
        direction = "down"
    else:
        direction = "flat"
    return priority, direction


def score_perpetual_kyc(engine: Any, examples: list[GoldenExample]) -> float:
    """Fraction of golden cases matching BOTH the expected queue place and re-score move.

    Priority alone is too coarse to catch an engine that queues correctly but never moves
    the score, so the golden set also declares the direction the re-score must take.
    """
    if not examples:
        return 0.0
    hits = sum(
        1
        for ex in examples
        if computed_pkyc_outcome(engine, ex)
        == (ex.expected_pkyc_priority, ex.expected_pkyc_direction)
    )
    return round(hits / len(examples), 4)


# --------------------------------------------------------------------------- #
# UBO-graph scorer
#
# Independent oracle: each golden case DECLARES the registry layers of a structure and,
# separately, the answer those layers must produce (the beneficial owners with their
# effective percentages, the control basis, the indicator kinds). The scorer replays the
# declared layers through the REAL engine and compares. It never reads the engine's own
# opinion of itself, so an engine that stops walking, mis-multiplies, or mis-orders the
# control ladder disagrees with the dataset and scores red.
# --------------------------------------------------------------------------- #
_UBO_AS_OF = date(2026, 8, 7)


class DeclaredRegistry:
    """Serves the golden case's declared layers as one cited hop at a time."""

    def __init__(self, example: GoldenExample) -> None:
        self._layers = list(example.ubo.get("layers") or ())
        self._meta = {
            str(e.get("name", "")): e
            for e in (example.ubo.get("entities") or ())
            if isinstance(e, dict)
        }
        self._subject_jurisdiction = example.jurisdiction

    def hop(self, entity_name: str, jurisdiction: str) -> RegistryHop:
        rows = [row for row in self._layers if row.get("owned") == entity_name]
        entity = self._node(entity_name, jurisdiction, "entity")
        if not rows:
            # No declared layer above this party: the registry has nothing to say, which
            # the engine reads as an opaque layer rather than as transparency.
            return RegistryHop(entity=entity, resolved=False)
        owners: list[OwnershipGraphNode] = []
        edges: list[OwnershipEdge] = []
        seen: set[str] = set()
        for row in rows:
            owner = self._node(
                str(row.get("owner", "")),
                str(row.get("owner_jurisdiction", "")),
                str(row.get("owner_kind", "entity")),
            )
            if owner.id not in seen:
                seen.add(owner.id)
                owners.append(owner)
            edges.append(
                OwnershipEdge(
                    source_id=owner.id,
                    target_id=entity.id,
                    kind=OwnershipEdgeKind(str(row.get("kind", "shareholding"))),
                    pct=float(row.get("pct", 0.0)),
                    citations=(self._citation(entity_name, owner.name),),
                )
            )
        return RegistryHop(entity=entity, owners=tuple(owners), edges=tuple(edges))

    def _node(self, name: str, jurisdiction: str, kind: str) -> OwnershipGraphNode:
        meta = self._meta.get(name, {})
        where = jurisdiction or str(meta.get("jurisdiction", "")) or self._subject_jurisdiction
        return OwnershipGraphNode(
            id=ownership_node_id(name, where),
            name=name,
            kind=OwnershipNodeKind(kind),
            jurisdiction=where,
            registered_address=str(meta.get("address", "")),
            incorporation_date=str(meta.get("incorporated", "")),
            status=str(meta.get("status", "")),
            citations=(self._citation(name, ""),),
        )

    @staticmethod
    def _citation(entity: str, owner: str) -> Citation:
        suffix = f":{ownership_node_id(owner)}" if owner else ""
        return Citation(
            source_id=f"golden-registry:{ownership_node_id(entity)}{suffix}",
            source_type=SourceType.REGISTRY,
            title=f"Declared registry layer for {entity} (FICTIONAL)",
            snippet="Golden-set fixture; entirely invented.",
        )


def computed_ubo_outcome(
    engine: Any, example: GoldenExample
) -> tuple[tuple[tuple[str, float], ...], str, tuple[str, ...]]:
    """Replay the declared layers through ``engine``: (owners, control basis, flags)."""
    subject = Subject(
        id=example.id,
        name=example.subject_name,
        type=SubjectType.ENTITY,
        jurisdiction=example.jurisdiction,
        tenant="golden-bank",
    )
    resolution = engine.resolve(
        subject=subject, as_of=_UBO_AS_OF, fetch=DeclaredRegistry(example).hop
    )
    owners = tuple(
        sorted((f.name, round(f.effective_pct, 4)) for f in resolution.beneficial_owners)
    )
    return owners, resolution.control_basis.value, resolution.flag_kinds


def expected_ubo_outcome(
    example: GoldenExample,
) -> tuple[tuple[tuple[str, float], ...], str, tuple[str, ...]]:
    """The golden case's own declaration, read straight from the dataset."""
    owners = tuple(
        sorted(
            (str(o.get("name", "")), round(float(o.get("effective_pct", 0.0)), 4))
            for o in (example.ubo.get("expected_owners") or ())
        )
    )
    return (
        owners,
        str(example.ubo.get("expected_control_basis", "none")),
        tuple(sorted(str(f) for f in (example.ubo.get("expected_flags") or ()))),
    )


def score_ubo_graph(engine: Any, examples: list[GoldenExample]) -> float:
    """Fraction of golden cases whose replay matches the case's own declaration.

    All three parts must match. Owners alone would pass an engine that finds the right
    people through the wrong arithmetic; the control basis alone would pass one that never
    walks past the first layer; the flags alone would pass one that finds nobody.
    """
    scored = [ex for ex in examples if ex.ubo]
    if not scored:
        return 0.0
    hits = sum(1 for ex in scored if computed_ubo_outcome(engine, ex) == expected_ubo_outcome(ex))
    return round(hits / len(scored), 4)


# --------------------------------------------------------------------------- #
# Report assembly
# --------------------------------------------------------------------------- #
@dataclass
class _PerMetric:
    scores: list[float] = field(default_factory=list)

    @property
    def mean(self) -> float:
        return sum(self.scores) / len(self.scores) if self.scores else 0.0


def run_offline(dataset: Path, thresholds: dict[str, float]) -> EvalReport:
    examples = load_golden(dataset)
    adapters = _build_adapters(examples)
    service = _make_service(adapters)

    agg: dict[str, _PerMetric] = {m: _PerMetric() for m in THRESHOLDS}
    print(f"Running offline eval gate over {len(examples)} golden cases (CddService).\n")
    for example in examples:
        case_input = _case_input(example)
        case = service.assess(case_input, actor="eval-bot")
        retrieved_ids = {
            p.citation.source_id
            for p in adapters.knowledge_base.search(
                RetrievalQuery(
                    text=f"... {example.subject_name} ...",
                    acl_principals=(f"case:{example.id}",),
                )
            )
        }
        agg["sow_groundedness"].scores.append(score_sow_groundedness(case))
        agg["citation_accuracy"].scores.append(score_citation_accuracy(case, retrieved_ids))
        agg["risk_band_accuracy"].scores.append(
            score_risk_band_accuracy(case, example.expected_risk_band)
        )
        agg["pii_safety"].scores.append(score_pii_safety(case, adapters.audit.events))

    # Perpetual KYC is scored over the whole set at once (one engine, replayed per case).
    engine = PerpetualKycEngine.from_policy(PerpetualKycPolicy())
    agg["pkyc_priority"].scores.append(score_perpetual_kyc(engine, examples))

    # The UBO graph is scored the same way: one engine built from the shipped policy,
    # replayed over every declared chain.
    ubo_engine = UboGraphEngine.from_policy(UboGraphPolicy(), CountryRiskPolicy())
    agg["ubo_accuracy"].scores.append(score_ubo_graph(ubo_engine, examples))

    results = tuple(
        EvalMetricResult(
            metric=metric,
            score=round(agg[metric].mean, 4),
            threshold=thresholds.get(metric, THRESHOLDS[metric]),
            passed=round(agg[metric].mean, 4) >= thresholds.get(metric, THRESHOLDS[metric]),
        )
        for metric in (
            "sow_groundedness",
            "risk_band_accuracy",
            "citation_accuracy",
            "pii_safety",
            "pkyc_priority",
            "ubo_accuracy",
        )
    )
    return EvalReport(dataset=str(dataset), results=results, n_examples=len(examples))


def run_gate(dataset: Path) -> tuple[EvalReport, bool]:
    """Route through the model-quality-gate promotion gate (EvaluationGatePort -> gcp/platform).

    The promotion verdict is the shared authority's, not this repo's: it calls
    ``evaluate`` (the scored EvalReport) then ``gate`` (the PASS/FAIL verdict), and the
    caller fails closed if EITHER is False. Requires ``CDD_PROFILE=platform|gcp`` so the
    offline smoke result is never relabelled a promotion verdict.
    """
    from cdd_sow_research.config import Settings, build_container

    settings = Settings.load()
    if settings.profile not in ("platform", "gcp"):
        raise SystemExit(
            "--mode gate is the promotion authority and requires CDD_PROFILE=platform or gcp "
            f"(got {settings.profile!r}); run --mode smoke for the offline pre-merge check."
        )
    container = build_container(settings)
    report = container.evaluation.evaluate(str(dataset))
    if not isinstance(report, EvalReport):  # pragma: no cover - defensive
        raise SystemExit("EvaluationGatePort.evaluate did not return an EvalReport")
    gate_passed = bool(container.evaluation.gate(str(dataset)))
    return report, gate_passed


def main(argv: list[str] | None = None) -> int:
    """Dispatch --mode via the shared eval_main scaffold (fail-closed exit codes).

    The offline smoke evaluator and the model-quality-gate runner below are this repo's own;
    eval_main
    provides the CLI, the aligned report rendering, and the fail-closed exit codes (gate mode
    exits 0 only when both the scored report and the authority's verdict pass).
    """
    return eval_main(
        smoke=lambda dataset: run_offline(dataset, load_thresholds_from_rubrics()),
        gate=run_gate,
        default_dataset=DEFAULT_DATASET,
        description="Offline / GCP evaluation gate for B1 (A4 / P-08).",
        smoke_label="offline heuristic (no GCP creds)",
        gate_label="model-quality-gate promotion gate (EvaluationGatePort)",
        argv=argv,
    )


if __name__ == "__main__":
    raise SystemExit(main())
