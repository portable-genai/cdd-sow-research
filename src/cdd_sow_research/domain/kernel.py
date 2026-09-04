"""Vertical-neutral domain kernel: the types ANY document-diligence vertical reuses.

This module is the reusable half of the domain split (see ARCHITECTURE.md, "Kernel vs
vertical"). Everything here is meaningful for any grounded, audited document-diligence
agent (credit memos, insurance claims, trade finance, ESG reviews, ...), not just
CDD/KYC: provenance (citations and retrieved passages), the LLM envelope, safety
(guardrail verdicts and PII redaction), session/memory, the WORM audit record, the
evaluation gate report, governance (A2A agent card, tool specs), knowledge-base ingest
results, and the shared severity scale.

A fork building a different vertical keeps this module untouched and rewrites only the
vertical artifact models (``models.py``). Like the rest of ``domain/`` it depends on
**nothing but the Python standard library** (P-02): every adapter family speaks these
types, which is what lets the managed stack be swapped without touching domain logic.

Backward compatibility: ``models.py`` re-exports every name defined here, so existing
imports (``from cdd_sow_research.domain.models import Citation``) keep working.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import UTC, datetime

# Sourced from the shared commons rather than redeclared here. Sixteen repositories had each
# hand-copied ``TokenUsage`` and the eval report types, and by the time anyone compared them they
# had drifted apart. Re-exporting retires that whole class of drift: there is exactly one
# definition to change, and ``tests/contract/test_port_parity.py`` asserts object IDENTITY (``is``)
# rather than structural conformance, because a look-alike copy satisfies a runtime_checkable
# Protocol and ``is`` does not.
#
# Both imports are zero-dependency stdlib-only modules, so the "domain depends on nothing but the
# standard library" rule above still holds in substance. ``agent_eval_kit.report`` is imported by
# SUBMODULE on purpose: the package root re-exports ``gate_client``, which pulls in httpx.
from agent_eval_kit.report import EvalMetricResult as EvalMetricResult
from agent_eval_kit.report import EvalReport as EvalReport
from hex_service_kit.observability import TokenUsage as TokenUsage


def utcnow() -> datetime:
    """Timezone-aware UTC now (the single clock the domain uses)."""
    return datetime.now(UTC)


# --------------------------------------------------------------------------- #
# Retrieval & citation (provenance)
# --------------------------------------------------------------------------- #
class SourceType(enum.Enum):
    """Every citation names which kind of evidence it points to."""

    DOCUMENT = "document"  # a case-document extract
    REGISTRY = "registry"  # a corporate-registry record
    MEDIA = "media"  # an adverse-media article
    REGULATION = "regulation"  # a regulatory expectation (via the compliance service)


@dataclass(frozen=True, slots=True)
class Citation:
    """Provenance attached to every generated claim in a dossier.

    Generalised across the evidence kinds the agent reasons over (documents,
    registries, media) plus regulatory expectations: a reviewer must be able to trace
    each statement back to its exact source and page.
    """

    source_id: str
    source_type: SourceType
    title: str
    url: str = ""
    page: int | None = None
    snippet: str = ""
    score: float | None = None


@dataclass(frozen=True, slots=True)
class RetrievedPassage:
    """A passage retrieved from the governed RAG store for a case."""

    text: str
    citation: Citation
    score: float = 0.0
    acl_tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RetrievalQuery:
    text: str
    top_k: int = 10
    acl_principals: tuple[str, ...] = ()  # case ACL principals for governed retrieval
    filters: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WebCitation:
    """Provenance for a public-web grounded fact (secondary, cross-border)."""

    title: str
    url: str
    snippet: str = ""


# --------------------------------------------------------------------------- #
# Generation (LLM)
# --------------------------------------------------------------------------- #
class ThinkingLevel(enum.Enum):
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True, slots=True)
class LlmMessage:
    role: str  # "user" | "model" | "system"
    content: str


@dataclass(frozen=True, slots=True)
class LlmRequest:
    messages: tuple[LlmMessage, ...]
    system_instruction: str | None = None
    model: str | None = None  # None => adapter default from config
    thinking: ThinkingLevel = ThinkingLevel.MEDIUM
    temperature: float = 0.0  # omitted at a call site means this value; it must not sample
    max_output_tokens: int = 4096
    response_schema: dict | None = None  # JSON schema for structured output


# ``TokenUsage`` was declared here (three int counters defaulting to zero). It now comes from
# ``hex_service_kit.observability``, imported at the top of this module: all sixteen copies were
# byte-identical, which is a shared value type that had simply never been shared.


@dataclass(frozen=True, slots=True)
class LlmResponse:
    text: str
    usage: TokenUsage = field(default_factory=TokenUsage)
    model: str = ""
    web_citations: tuple[WebCitation, ...] = ()
    raw: dict | None = None


# --------------------------------------------------------------------------- #
# Safety (guardrail + PII redaction) — agent-guardrail-gateway concerns (rule R1)
# --------------------------------------------------------------------------- #
class GuardrailCategory(enum.Enum):
    PROMPT_INJECTION = "prompt_injection"
    JAILBREAK = "jailbreak"
    SENSITIVE_DATA = "sensitive_data"
    MALICIOUS_URL = "malicious_url"
    HATE = "hate"
    HARASSMENT = "harassment"
    SEXUAL = "sexual"
    DANGEROUS = "dangerous"
    OTHER = "other"


class Direction(enum.Enum):
    INPUT = "input"
    OUTPUT = "output"


@dataclass(frozen=True, slots=True)
class GuardrailFinding:
    category: GuardrailCategory
    confidence: str  # e.g. "low" | "medium" | "high"
    detail: str = ""


@dataclass(frozen=True, slots=True)
class GuardrailVerdict:
    allowed: bool
    direction: Direction
    findings: tuple[GuardrailFinding, ...] = ()
    # Text after any inline sanitisation the guardrail applied (may equal input).
    sanitized_text: str | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class RedactionFinding:
    info_type: str  # e.g. "PERSON_NAME", "SG_NRIC_FIN", "PASSPORT_NUMBER"
    count: int = 1


@dataclass(frozen=True, slots=True)
class RedactionResult:
    text: str  # de-identified text safe to send to the model / audit log
    findings: tuple[RedactionFinding, ...] = ()

    @property
    def redacted(self) -> bool:
        return bool(self.findings)


# --------------------------------------------------------------------------- #
# Runtime, session & memory
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Session:
    id: str
    user_id: str
    case_id: str | None = None
    created_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True, slots=True)
class MemoryItem:
    id: str
    content: str
    scope: str = "user"  # "user" | "case" | "global"
    created_at: datetime = field(default_factory=utcnow)


# --------------------------------------------------------------------------- #
# Audit & observability — agent-observability & Audit concerns (rule R2)
# --------------------------------------------------------------------------- #
class Decision(enum.Enum):
    ALLOWED = "allowed"
    BLOCKED = "blocked"
    ESCALATED = "escalated"  # routed to a human (maker-checker)


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """An immutable, WORM-stored Audit Event v2.

    Prompt and response are stored **already redacted** (P-04): customer PII is removed
    at the boundary before it is ever written to the audit sink or a trace span.
    """

    action: str  # e.g. "assess_cdd" | "source_of_wealth" | "adverse_media" | "ownership"
    actor: str  # authenticated analyst / service identity
    decision: Decision
    redacted_prompt: str
    redacted_response: str
    citations: tuple[Citation, ...] = ()
    resource: str = "cdd-sow-research"
    trace_id: str | None = None
    span_id: str | None = None
    correlation_id: str | None = None
    run_id: str | None = None
    event_id: str = ""
    schema_version: str = "audit-event/v2"
    timestamp: datetime = field(default_factory=utcnow)
    metadata: dict[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Evaluation gate — model-quality-gate concerns (rule R5)
# --------------------------------------------------------------------------- #
# ``EvalMetricResult`` and ``EvalReport`` were declared here. They now come from
# ``agent_eval_kit.report``, imported at the top of this module. The commons ``EvalReport`` is a
# strict SUPERSET: the same ``dataset`` / ``results`` / ``n_examples`` fields with the same
# fail-closed ``passed`` rule (a report over zero examples or zero metrics never passes), plus
# defaulted provenance fields (``run_id``, ``dataset_version``, ``dataset_digest``, ``evaluator``,
# ``schema_version``, ``trace_id``, ``correlation_id``, ``artifact_refs``, ``attested``). Every
# existing constructor call still compiles, and the remote adapter now returns that evidence
# instead of discarding it.


# --------------------------------------------------------------------------- #
# Governance — agent-registry concerns (A2A AgentCard, rule R4)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class AgentSkill:
    id: str
    name: str
    description: str


@dataclass(frozen=True, slots=True)
class AgentCard:
    """Minimal A2A-style agent card published at /.well-known/agent-card.json."""

    name: str
    description: str
    url: str
    version: str
    skills: tuple[AgentSkill, ...] = ()
    provider: str = "cdd-sow-research"


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """A governed, least-privilege tool exposed to the agent (typically via MCP)."""

    name: str
    description: str
    input_schema: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Knowledge-base ingestion (the case's governed RAG store IS enterprise-knowledge-base)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class IngestResult:
    document_id: str
    chunks: int = 0
    status: str = "indexed"
    ok: bool = True
    detail: str = ""


# --------------------------------------------------------------------------- #
# Shared severity scale
# --------------------------------------------------------------------------- #
class Severity(enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"
