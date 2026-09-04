"""Vertical domain models for the CDD + Source-of-Wealth Agent (catalog id cdd-sow-research).

The domain models are split in two (see ARCHITECTURE.md, "Kernel vs vertical"):

* ``kernel.py`` holds the **vertical-neutral kernel**: provenance (citations), the LLM
  envelope, safety (guardrail/redaction), the WORM audit record, the eval report,
  governance types and the shared severity scale. Any document-diligence vertical
  (credit memos, claims, trade finance) reuses the kernel untouched.
* This module holds the **CDD/SoW vertical artifacts**: the KYC inputs, the dossier,
  the long-running SoW case, screening, scorecard, Source-of-Funds and monitoring.
  A fork building a different vertical rewrites THIS module (and the services that
  produce these artifacts), not the kernel or the engines.

Taxonomy note: the wealth-source / funds-origin / document-type vocabularies are
``enum.StrEnum`` classes whose members ARE their string values. The deterministic
engines (``gap_analysis.py``, ``source_of_funds_service.py``) are typed on plain ``str``
kinds, so a deployment can extend the taxonomy through configuration (or a fork can
replace these enums) without editing the engine code. Serialized JSON values are the
enum string values either way.

Like the kernel, this module has **no dependency on Google Cloud, ADK, FastAPI, or any
framework** (only the Python standard library): every adapter speaks these types, which
is what lets the managed stack be swapped for an on-premise one without touching domain
logic (General Principle P-02). PII is redacted at the boundary before it ever reaches a
model, a trace span, or the audit sink (P-04, rule R1).

Every kernel name is re-exported here for backward compatibility, so
``from cdd_sow_research.domain.models import Citation`` keeps working.
"""

from __future__ import annotations

import enum
import hashlib
from dataclasses import dataclass, field
from datetime import datetime

# Vertical-neutral kernel types, re-exported for backward compatibility: existing code
# imports them from models; new code may import them from kernel directly.
from .kernel import (  # noqa: F401
    AgentCard,
    AgentSkill,
    AuditEvent,
    Citation,
    Decision,
    Direction,
    EvalMetricResult,
    EvalReport,
    GuardrailCategory,
    GuardrailFinding,
    GuardrailVerdict,
    IngestResult,
    LlmMessage,
    LlmRequest,
    LlmResponse,
    MemoryItem,
    RedactionFinding,
    RedactionResult,
    RetrievalQuery,
    RetrievedPassage,
    Session,
    Severity,
    SourceType,
    ThinkingLevel,
    TokenUsage,
    ToolSpec,
    WebCitation,
    utcnow,
)


# --------------------------------------------------------------------------- #
# Subject & KYC inputs
# --------------------------------------------------------------------------- #
class SubjectType(enum.Enum):
    """A CDD subject is either a natural person or a corporate entity."""

    INDIVIDUAL = "individual"
    ENTITY = "entity"


@dataclass(frozen=True, slots=True)
class Subject:
    """The customer the dossier is being built for (individual or entity)."""

    id: str  # stable case-scoped subject id, e.g. "subj-acme-holdings"
    name: str
    type: SubjectType = SubjectType.INDIVIDUAL
    jurisdiction: str = ""  # ISO-ish country/region code, e.g. "SG", "HK"
    dob_or_incorp: str | None = None  # ISO date of birth or incorporation
    tenant: str = ""  # owning tenant; stamped into the case-store ACL for isolation


class DocType(enum.StrEnum):
    """KYC document kinds the agent extracts and indexes (the reference taxonomy).

    A ``StrEnum``: members are their string values, so policy tables and engines can be
    keyed by plain strings and a fork can extend the vocabulary without engine edits.
    """

    PASSPORT = "passport"
    FIN_STATEMENT = "fin_statement"
    REGISTRY_EXTRACT = "registry_extract"
    BANK_STATEMENT = "bank_statement"
    OTHER = "other"


def document_id(content: bytes, subject_id: str, doc_type: DocType, filename: str) -> str:
    """The id a stored case document is given: derived from the FILING, not minted.

    ``KycDocument.id`` is documented as "stable doc id within the case" and was a fresh
    ``uuid4`` on every upload, so the same evidence uploaded twice became two documents.
    Nothing deduplicated them afterwards, so a case's corpus grew by one copy per upload
    and every retrieval cited the same page as though it were several independent sources
    -- the one failure mode a citation count exists to rule out. The managed store had
    accumulated eight copies of a single synthetic bank statement before a paired run
    counted them.

    What makes two uploads the same document is the whole filing and not the bytes alone.
    Scoping by ``subject_id`` keeps idempotence from becoming SHARING: identical bytes
    filed under two subjects stay two documents with two ACLs, because collapsing them
    would let one case's access decision reach another's evidence. ``doc_type`` and
    ``filename`` are in the digest for the converse reason -- the same bytes deliberately
    filed twice, as a statement and as a registry extract, are two filings, and merging
    them would silently discard one.
    """
    parts = (subject_id, doc_type.value, filename)
    digest = hashlib.sha256(b"\x00".join(p.encode("utf-8") for p in parts) + b"\x00" + content)
    return f"doc-{digest.hexdigest()[:12]}"


def citation_title(doc_type: DocType) -> str:
    """The name a cited document carries in a dossier, in EVERY profile.

    The stable cross-profile identity of a cited document is its TITLE, never its id: the
    same document ingested into a laptop store and into the managed store is minted a
    different id in each, so an id cannot name a source across profiles. That makes this
    the one place the name is decided, and a second store cannot answer differently.

    It exists because one did. The managed knowledge base wrote no title at ingest and
    read ``source_id`` back as the title at search, so every managed citation decayed into
    its own opaque id: a dossier grounded in ``doc-c9dba9861a1f`` rather than in the bank
    statement. The link still resolved, which is why nothing failed and nobody noticed --
    the evidence relationship had degraded while every check stayed green.
    """
    return doc_type.value


@dataclass(frozen=True, slots=True)
class KycDocument:
    """A KYC document supplied for a case (the raw source for extraction/indexing)."""

    id: str  # stable doc id within the case
    doc_type: DocType = DocType.OTHER
    uri: str = ""  # where the bytes live (object store / case vault)
    acl_tags: tuple[str, ...] = ()  # case-scoped access-control tags


@dataclass(frozen=True, slots=True)
class DocumentExtract:
    """Structured + raw text extracted from a KYC document (from Document AI).

    ``page_texts`` carries the SAME text split per source page, one entry per page in
    document order. It is what lets an ingesting knowledge base cite the page a claim
    actually came from ("p.11") rather than the whole file; extractors that cannot
    recover page boundaries leave it empty and callers fall back to ``text``.
    """

    document_id: str
    fields: dict[str, str] = field(default_factory=dict)  # key/value form fields
    text: str = ""  # full extracted text
    pages: int = 0
    page_texts: tuple[str, ...] = ()  # per-page text, page N == page_texts[N - 1]


@dataclass(frozen=True, slots=True)
class StoredDocument:
    """A user-uploaded case document held in the document store (metadata only).

    The bytes live in the :class:`~cdd_sow_research.ports.document_store.DocumentStorePort`
    adapter; this is the record a reviewer sees and the handle a citation points at.
    ``acl_tags`` mirrors the knowledge-base ACL contract exactly (subset match,
    fail-closed), so a document is readable only by a caller holding every tag.
    """

    id: str  # stable, server-minted document id
    filename: str = ""  # the uploader's original filename (display only)
    doc_type: DocType = DocType.OTHER
    mime_type: str = "application/pdf"
    size_bytes: int = 0
    pages: int = 0  # 0 until extraction has counted them
    subject_id: str = ""  # the case/subject this document was uploaded for
    acl_tags: tuple[str, ...] = ()  # case + tenant tags; subset match, fail-closed
    uploaded_at: str = ""  # ISO-8601 UTC
    sha256: str = ""  # content digest (integrity + de-duplication)

    @property
    def uri(self) -> str:
        """The API-relative path serving these bytes back (what a citation links to).

        Deliberately relative: the same dossier JSON is served standalone and embedded
        behind a portal path prefix, so the UI resolves it against its own API base
        rather than the backend guessing its public origin.

        The case is in the path, not just the document id, because that is what lets the
        serving route derive the reader's ACL from the verified principal BEFORE it
        touches the store. Without it, the server would have to read a document's
        metadata to discover which case it belongs to, which is the read it is trying to
        authorize.
        """
        from urllib.parse import quote

        return f"/v1/cases/{quote(self.subject_id, safe='')}/documents/{quote(self.id, safe='')}"

    def to_kyc_document(self) -> KycDocument:
        """The pipeline's view of this upload (id, kind, where the bytes are, ACL)."""
        return KycDocument(
            id=self.id,
            doc_type=self.doc_type,
            uri=self.uri,
            acl_tags=self.acl_tags,
        )


# --------------------------------------------------------------------------- #
# 1. Source-of-Wealth narrative
# --------------------------------------------------------------------------- #
class WealthSourceKind(enum.StrEnum):
    """How a tranche of the subject's wealth was generated (the reference taxonomy)."""

    EMPLOYMENT = "employment"
    BUSINESS_OWNERSHIP = "business_ownership"
    INHERITANCE = "inheritance"
    INVESTMENTS = "investments"
    ASSET_SALE = "asset_sale"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class WealthSource:
    """One corroborated source of the subject's wealth."""

    kind: str  # a WealthSourceKind value, or a deployment-specific extension
    description: str
    est_value_band: str = ""  # e.g. "USD 1m-5m" (a band, never a spurious precise figure)
    citations: tuple[Citation, ...] = ()


@dataclass(frozen=True, slots=True)
class SourceOfWealthNarrative:
    """A cited narrative explaining how the subject's wealth was generated."""

    subject_id: str
    narrative: str
    sources: tuple[WealthSource, ...] = ()
    citations: tuple[Citation, ...] = ()
    confidence: float = 0.0
    requires_human_review: bool = True


# --------------------------------------------------------------------------- #
# 2. Risk rating
# --------------------------------------------------------------------------- #
class RiskBand(enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    PROHIBITED = "prohibited"


@dataclass(frozen=True, slots=True)
class RiskFactor:
    """One weighted contributor to the overall risk rating."""

    name: str  # e.g. "pep_exposure", "high_risk_jurisdiction", "adverse_media"
    weight: float
    present: bool
    detail: str = ""
    citations: tuple[Citation, ...] = ()


@dataclass(frozen=True, slots=True)
class RiskRating:
    """An overall risk band with weighted factors and a written rationale."""

    band: RiskBand
    score: float
    factors: tuple[RiskFactor, ...] = ()
    rationale: str = ""
    citations: tuple[Citation, ...] = ()
    requires_human_review: bool = True


# --------------------------------------------------------------------------- #
# 3. Adverse-media findings
# --------------------------------------------------------------------------- #
class AdverseMediaCategory(enum.Enum):
    FRAUD = "fraud"
    CORRUPTION = "corruption"
    SANCTIONS = "sanctions"
    MONEY_LAUNDERING = "money_laundering"
    TERRORISM = "terrorism"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class AdverseMediaFinding:
    """A negative-news hit from public-web grounding."""

    headline: str
    publisher: str
    url: str
    published_date: str | None = None  # ISO date as published
    category: AdverseMediaCategory = AdverseMediaCategory.OTHER
    severity: Severity = Severity.MEDIUM
    snippet: str = ""
    citation: Citation | None = None


@dataclass(frozen=True, slots=True)
class AdverseMediaScreening:
    """An adverse-media screen, carrying the provenance of the search as well as its result.

    The presence of this object answers "was the subject screened". ``findings`` answers
    "what did the screen return". Those are two different facts and a bare list of findings
    can only carry one of them: an empty list is what a clean screen and an unreachable
    backend both look like, and a console renders the pair identically.

    So a port with no reachable source returns ``None`` rather than an empty screening, and
    a case whose ``adverse_media`` is ``None`` was never screened. This is the same
    distinction :class:`ScreeningResult` already draws for watchlist screening.
    """

    subject_name: str
    findings: tuple[AdverseMediaFinding, ...] = ()
    #: What was actually consulted, so a screen with no findings is inspectable.
    sources: tuple[str, ...] = ()
    searched_at: datetime = field(default_factory=utcnow)


# --------------------------------------------------------------------------- #
# 4. Ownership / UBO summary
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class BeneficialOwner:
    """A natural person (or holding) with a beneficial interest in the subject."""

    name: str
    pct: float  # beneficial ownership percentage
    country: str = ""
    is_pep: bool = False
    citations: tuple[Citation, ...] = ()


@dataclass(frozen=True, slots=True)
class OwnershipNode:
    """One node in the corporate-ownership tree."""

    entity_name: str
    pct: float = 100.0
    children: tuple[OwnershipNode, ...] = ()


@dataclass(frozen=True, slots=True)
class OwnershipSummary:
    """The corporate-ownership / UBO picture from corporate-registry extracts.

    A FLAT, one-hop picture: who the beneficial owners are and what they hold. It stays
    the shape the dossier (:class:`CDDCase`) and the related-party derivation consume, so
    the cross-jurisdiction graph below is a strictly additive capability that converts
    DOWN to this (``cdd_sow_research.domain.ubo_graph.to_ownership_summary``) rather than
    replacing it.
    """

    root_entity: str
    owners: tuple[BeneficialOwner, ...] = ()
    tree: OwnershipNode | None = None
    citations: tuple[Citation, ...] = ()


# --------------------------------------------------------------------------- #
# 4b. The cross-jurisdiction UBO graph
#
# The summary above answers "who are the owners of record". A layered structure answers
# it only after the chain is walked: a natural person holding 60% of a Jersey holding
# company that holds 50% of the operating entity is a 30% beneficial owner nobody's
# one-hop extract shows. These types carry that walk.
#
# Every value below is produced by the PURE engine (``domain/ubo_graph.py``) from cited
# registry hops plus bank-owned policy. **The LLM never emits a node, an edge or a
# percentage**: it narrates the finished resolution and nothing else. A resolution is
# consequential decision support, so it always sets ``requires_human_review`` and is
# routed to human-review-console (rule R8); it never auto-blocks and never concludes.
# --------------------------------------------------------------------------- #
class OwnershipNodeKind(enum.StrEnum):
    """What a node in the ownership graph IS (drives who can be a UBO)."""

    ENTITY = "entity"  # a body corporate: a layer, never itself the UBO
    NATURAL_PERSON = "natural_person"  # the only kind that can BE a beneficial owner
    TRUST = "trust"  # a trust/foundation arrangement
    NOMINEE = "nominee"  # a declared nominee/fiduciary holder
    STATE = "state"  # a government or state body
    LISTED = "listed"  # a listed company (the regulated-market carve-out)
    UNKNOWN = "unknown"  # the registry did not say


class OwnershipEdgeKind(enum.StrEnum):
    """How one party is connected to another (the five recognised control routes)."""

    SHAREHOLDING = "shareholding"  # equity: the only kind multiplied into effective %
    VOTING = "voting"  # voting rights, which can diverge from equity
    DIRECTORSHIP = "directorship"  # a board seat
    NOMINEE_ARRANGEMENT = "nominee_arrangement"  # holding on another party's behalf
    CONTRACTUAL = "contractual"  # control by agreement (shareholder pact, golden share)


class ControlBasis(enum.StrEnum):
    """Which rung of the control ladder a finding was established on.

    Declared in ladder order: the engine tries each in turn and stops at the first that
    holds, so the basis a resolution reports is always the strongest one available.
    """

    EFFECTIVE_OWNERSHIP = "effective_ownership"
    VOTING_MAJORITY = "voting_majority"
    BOARD_MAJORITY = "board_majority"
    CONTRACTUAL = "contractual"
    SENIOR_MANAGING_OFFICIAL = "senior_managing_official"  # the FALLBACK, never a finding
    NONE = "none"


class OwnershipFlagKind(enum.StrEnum):
    """A deterministic INDICATOR raised by the engine. Never a conclusion."""

    NOMINEE_INDICATOR = "nominee_indicator"
    SHELL_INDICATOR = "shell_indicator"
    CIRCULAR_HOLDING = "circular_holding"
    DEPTH_TRUNCATED = "depth_truncated"
    SECRECY_JURISDICTION = "secrecy_jurisdiction"
    UNRESOLVED_LAYER = "unresolved_layer"
    NO_OWNER_AT_THRESHOLD = "no_owner_at_threshold"


@dataclass(frozen=True, slots=True)
class OwnershipGraphNode:
    """One party in the ownership graph, as a registry recorded it.

    ``id`` is the stable traversal key (see ``ubo_graph.ownership_node_id``): the same
    party in the same jurisdiction always yields the same id, so a cycle is detectable
    and a run replays byte for byte.
    """

    id: str
    name: str
    kind: OwnershipNodeKind = OwnershipNodeKind.UNKNOWN
    jurisdiction: str = ""
    registered_address: str = ""
    incorporation_date: str = ""  # ISO date as filed
    status: str = ""  # registry filing status, e.g. "active" / "dormant"
    is_pep: bool = False
    depth: int = 0  # hops from the subject; the subject itself is 0
    citations: tuple[Citation, ...] = ()


@dataclass(frozen=True, slots=True)
class OwnershipEdge:
    """One recorded connection, directed from the OWNER to the party owned/controlled."""

    source_id: str  # the owner / controller
    target_id: str  # the entity owned or controlled
    kind: OwnershipEdgeKind = OwnershipEdgeKind.SHAREHOLDING
    pct: float = 0.0  # percentage for shareholding/voting; 0 for the other kinds
    as_of: str = ""  # ISO date the registry recorded it
    citations: tuple[Citation, ...] = ()


@dataclass(frozen=True, slots=True)
class RegistryHop:
    """ONE cited registry answer: the parties recorded directly against one entity.

    The unit the :class:`~cdd_sow_research.ports.ownership_graph.OwnershipGraphPort` returns.
    Traversal is deliberately NOT here and not in the adapter: the engine decides which
    hop to ask for next, so the depth limit, the visited set and the truncation flag are
    all bank-owned policy applied in pure code.
    """

    entity: OwnershipGraphNode
    owners: tuple[OwnershipGraphNode, ...] = ()
    edges: tuple[OwnershipEdge, ...] = ()
    citations: tuple[Citation, ...] = ()
    resolved: bool = True  # False when the registry could not answer (an opaque layer)


@dataclass(frozen=True, slots=True)
class OwnershipGraph:
    """The walked, cross-jurisdiction ownership structure behind one subject."""

    root_id: str
    root_name: str = ""
    nodes: tuple[OwnershipGraphNode, ...] = ()
    edges: tuple[OwnershipEdge, ...] = ()
    depth: int = 0  # the deepest layer actually reached
    truncated: bool = False  # a limit stopped the walk: the picture is incomplete
    unresolved_ids: tuple[str, ...] = ()  # layers the registry could not answer for
    jurisdictions: tuple[str, ...] = ()  # every jurisdiction touched, sorted
    as_of: str = ""  # ISO date the walk was evaluated for (drives replayability)

    def node_for(self, node_id: str) -> OwnershipGraphNode | None:
        """The node behind an id, or ``None`` when the graph does not carry it."""
        for node in self.nodes:
            if node.id == node_id:
                return node
        return None

    @property
    def citations(self) -> tuple[Citation, ...]:
        """Every citation behind the graph, de-duplicated, in node-then-edge order."""
        seen: set[str] = set()
        out: list[Citation] = []
        for citation in (
            *(c for node in self.nodes for c in node.citations),
            *(c for edge in self.edges for c in edge.citations),
        ):
            if citation.source_id in seen:
                continue
            seen.add(citation.source_id)
            out.append(citation)
        return tuple(out)


@dataclass(frozen=True, slots=True)
class OwnershipPathStep:
    """One hop of a path, carrying the percentage that hop contributes."""

    source_id: str
    target_id: str
    source_name: str = ""
    target_name: str = ""
    kind: OwnershipEdgeKind = OwnershipEdgeKind.SHAREHOLDING
    pct: float = 0.0


@dataclass(frozen=True, slots=True)
class OwnershipPath:
    """One SIMPLE path from an owner down to the subject, with its own arithmetic.

    ``arithmetic`` is the multiplication rendered for a human: showing the working is
    this repo's convention, and it is what lets a reviewer check the number by hand.
    """

    steps: tuple[OwnershipPathStep, ...] = ()
    product_pct: float = 0.0
    arithmetic: str = ""
    citations: tuple[Citation, ...] = ()


@dataclass(frozen=True, slots=True)
class UboFinding:
    """One candidate beneficial owner or controller, with the paths that produced it."""

    node_id: str
    name: str
    kind: OwnershipNodeKind = OwnershipNodeKind.UNKNOWN
    jurisdiction: str = ""
    is_pep: bool = False
    effective_pct: float = 0.0  # sum over simple paths of the product of shareholdings
    paths: tuple[OwnershipPath, ...] = ()
    control_basis: ControlBasis = ControlBasis.NONE
    control_reason: str = ""
    meets_threshold: bool = False  # a natural person at/above the policy ownership %
    citations: tuple[Citation, ...] = ()


@dataclass(frozen=True, slots=True)
class OwnershipFlag:
    """A deterministic indicator with the reason and the evidence behind it."""

    kind: OwnershipFlagKind
    severity: Severity = Severity.MEDIUM
    node_id: str = ""
    node_name: str = ""
    summary: str = ""
    detail: str = ""
    citations: tuple[Citation, ...] = ()


@dataclass(frozen=True, slots=True)
class UboResolution:
    """The deterministic outcome of one UBO-graph resolution for a subject."""

    subject_id: str
    subject_name: str = ""
    tenant: str = ""
    as_of: str = ""  # ISO date the run was evaluated for (drives replayability)
    graph: OwnershipGraph | None = None
    findings: tuple[UboFinding, ...] = ()
    control_basis: ControlBasis = ControlBasis.NONE
    control_rationale: str = ""
    flags: tuple[OwnershipFlag, ...] = ()
    opacity_score: float = 0.0  # 0..1; deterministic sum of the indicator weights
    ownership_threshold_pct: float = 25.0  # the policy threshold this run applied
    rationale: str = ""
    narrative: str = ""  # LLM prose; carries no number the code did not compute
    requires_human_review: bool = True
    routed_to_hrz7: bool = False
    acl: tuple[str, ...] = ()  # server-derived tags; never client-supplied
    generated_at: datetime = field(default_factory=utcnow)

    @property
    def beneficial_owners(self) -> tuple[UboFinding, ...]:
        """The natural persons at or above the policy ownership threshold."""
        return tuple(f for f in self.findings if f.meets_threshold)

    @property
    def controllers(self) -> tuple[UboFinding, ...]:
        """Every finding that established control on some rung of the ladder."""
        return tuple(f for f in self.findings if f.control_basis is not ControlBasis.NONE)

    @property
    def flag_kinds(self) -> tuple[str, ...]:
        """The distinct indicator kinds raised, sorted (the stable comparison key)."""
        return tuple(sorted({f.kind.value for f in self.flags}))

    @property
    def citations(self) -> tuple[Citation, ...]:
        """Every citation behind the findings and flags, de-duplicated, in order."""
        seen: set[str] = set()
        out: list[Citation] = []
        for citation in (
            *(c for finding in self.findings for c in finding.citations),
            *(c for flag in self.flags for c in flag.citations),
        ):
            if citation.source_id in seen:
                continue
            seen.add(citation.source_id)
            out.append(citation)
        return tuple(out)


# --------------------------------------------------------------------------- #
# The CDD dossier (the bundled top-level artifact)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class CaseInput:
    """The inbound request to assess a case: a subject plus its KYC documents."""

    subject: Subject
    documents: tuple[KycDocument, ...] = ()


@dataclass(frozen=True, slots=True)
class CDDCase:
    """A single CDD dossier bundling all four cited, audited artifacts.

    A CDD dossier is consequential, so it **always** requires human review (maker
    checker, P-06): a maker (the agent) proposes and a checker (a qualified analyst /
    MLRO) disposes before it is relied upon.
    """

    id: str
    subject: Subject
    sow: SourceOfWealthNarrative
    rating: RiskRating
    # None means no adverse-media search ran (no reachable backend, or the port refused),
    # which is distinct from "searched and clear" (a screening with zero findings). The
    # console must not render the first as the second.
    adverse_media: AdverseMediaScreening | None = None
    ownership: OwnershipSummary | None = None
    # Deterministic watchlist screening against the synced point-in-time snapshot.
    # None means the case was not screened (no provider wired), which is distinct from
    # "screened and clear" (a result with zero alerts).
    # Quotes are load-bearing: ScreeningResult is defined later in this module and
    # annotations here are eager (no future import), so unquoting would NameError at
    # import time on py312; the UP037 suppression below keeps ruff from "fixing" that.
    screening: "ScreeningResult | None" = None  # noqa: UP037
    requires_human_review: bool = True
    generated_at: datetime = field(default_factory=utcnow)


# --------------------------------------------------------------------------- #
# Long-running, auditable Source-of-Wealth cases
# (see docs/sow-longitudinal-audit-design.md)
#
# A SoW case is not one-shot: an RM closes evidence gaps with the client over weeks,
# evidence accrues in append-only iterations, and the output is an audit artifact
# (sources grouped, each proven by a Citation, with deterministic reconciliation,
# computed gaps, suggested changes, and client information requests).
# --------------------------------------------------------------------------- #
class CaseStatus(enum.Enum):
    """The explicit state of a long-running SoW case (the state machine)."""

    DRAFT = "draft"
    GATHERING = "gathering"
    ANALYSING = "analysing"
    RFI_PENDING = "rfi_pending"
    READY_FOR_REVIEW = "ready_for_review"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    BLOCKED = "blocked"
    ON_HOLD = "on_hold"
    WITHDRAWN = "withdrawn"


class GapKind(enum.Enum):
    """A computed shortfall the deterministic gap engine can detect."""

    MISSING_CORROBORATION = "missing_corroboration"
    UNRECONCILED_DELTA = "unreconciled_delta"
    STALE_EVIDENCE = "stale_evidence"
    MISSING_MANDATORY_DOC = "missing_mandatory_doc"
    INCONSISTENT_VALUE = "inconsistent_value"
    UNVERIFIED_PEP_LINK = "unverified_pep_link"


class RfiStatus(enum.Enum):
    """Lifecycle of a client information request (RFI)."""

    DRAFT = "draft"
    SENT = "sent"
    PARTIALLY_ANSWERED = "partially_answered"
    ANSWERED = "answered"
    OVERDUE = "overdue"
    WAIVED = "waived"


@dataclass(frozen=True, slots=True)
class DeclaredSource:
    """A wealth source the client *claims*, captured in the SoW declaration at open.

    This is the 'claimed' side of every reconciliation line; without it the
    declared-vs-evidenced math has no left-hand side.
    """

    kind: str  # a WealthSourceKind value, or a deployment-specific extension
    description: str = ""
    declared_band: str = ""  # what the client says it is worth, e.g. "USD 25m-50m"


@dataclass(frozen=True, slots=True)
class WealthDeclaration:
    """The client's self-declared Source of Wealth, captured when the case opens."""

    sources: tuple[DeclaredSource, ...] = ()
    declared_net_worth_band: str = ""
    captured_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    """One source ever submitted to the case, with provenance and arrival round."""

    id: str
    document: KycDocument
    extract: DocumentExtract | None = None
    ingest: IngestResult | None = None
    iteration_no: int = 0  # which round it arrived in
    received_at: datetime = field(default_factory=utcnow)
    doc_date: str | None = None  # ISO date the document itself is dated (for staleness)
    provided_by: str = ""  # "client" | RM identity
    supports_kinds: tuple[str, ...] = ()  # WealthSourceKind values (or extensions)
    evidenced_band: str = ""  # value this item attests, e.g. "USD 25m-50m"
    idempotency_key: str = ""
    citations: tuple[Citation, ...] = ()

    @property
    def corroborated(self) -> bool:
        """An item corroborates a source when it carries at least one citation."""
        return bool(self.citations)


@dataclass(frozen=True, slots=True)
class ReconciliationLine:
    """One declared-vs-evidenced row of the deterministic reconciliation."""

    kind: str  # the wealth-source kind this row reconciles
    declared_band: str = ""
    evidenced_band: str = ""
    delta_note: str = ""  # human-readable computed delta
    coverage_pct: float = 0.0  # evidenced / declared for this source
    corroborated: bool = False
    citations: tuple[Citation, ...] = ()


@dataclass(frozen=True, slots=True)
class WealthReconciliation:
    """The deterministic claimed-vs-evidenced calculation (no LLM)."""

    lines: tuple[ReconciliationLine, ...] = ()
    declared_total_band: str = ""
    evidenced_total_band: str = ""
    coverage_pct: float = 0.0  # evidenced / declared, total
    consistency_notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Gap:
    """A computed gap (deterministic), ranked by severity."""

    id: str
    kind: GapKind
    severity: Severity
    summary: str
    related_kind: str | None = None  # the wealth-source kind implicated, if any
    evidence_ids: tuple[str, ...] = ()  # ledger items implicated
    detail: str = ""  # the calculation behind it


@dataclass(frozen=True, slots=True)
class InformationRequest:
    """A client-ready ask (RFI), linked to the gap it closes."""

    id: str
    gap_id: str
    ask: str  # client-ready wording
    suggested_doc_types: tuple[str, ...] = ()  # free-form; DocType is too coarse
    priority: Severity = Severity.MEDIUM
    status: RfiStatus = RfiStatus.DRAFT
    due_date: str | None = None
    created_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True, slots=True)
class SuggestedChange:
    """A proposed edit to the narrative / classification / value band."""

    target: str  # "narrative" | "source:<kind>" | "value_band"
    rationale: str
    proposed: str
    citations: tuple[Citation, ...] = ()


@dataclass(frozen=True, slots=True)
class SourceGroup:
    """All evidence supporting one wealth source, grouped for at-a-glance audit."""

    kind: str  # the wealth-source kind this group covers
    declared_band: str = ""
    evidenced_band: str = ""
    corroborated: bool = False
    evidence_ids: tuple[str, ...] = ()
    citations: tuple[Citation, ...] = ()


@dataclass(frozen=True, slots=True)
class SowAuditView:
    """The audit-first output: groups, proofs, calculations, gaps, suggestions, RFIs."""

    subject_id: str
    narrative: SourceOfWealthNarrative
    groups: tuple[SourceGroup, ...] = ()
    reconciliation: WealthReconciliation = field(default_factory=WealthReconciliation)
    gaps: tuple[Gap, ...] = ()
    suggested_changes: tuple[SuggestedChange, ...] = ()
    rfis: tuple[InformationRequest, ...] = ()
    related_parties: RelatedPartyReview | None = None
    screening: ScreeningResult | None = None
    scorecard: RiskScorecard | None = None
    source_of_funds: SourceOfFundsAssessment | None = None
    monitoring: MonitoringAssessment | None = None
    generated_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True, slots=True)
class SowIteration:
    """One append-only round of the case (evidence added + analysis produced)."""

    no: int
    added_evidence_ids: tuple[str, ...] = ()
    audit_view: SowAuditView | None = None
    rfi_ids: tuple[str, ...] = ()
    actor: str = ""
    created_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True, slots=True)
class SowCase:
    """The long-lived aggregate root for a Source-of-Wealth case."""

    id: str
    subject: Subject
    status: CaseStatus = CaseStatus.DRAFT
    version: int = 0  # optimistic concurrency
    declaration: WealthDeclaration | None = None
    ledger: tuple[EvidenceItem, ...] = ()  # append-only evidence
    iterations: tuple[SowIteration, ...] = ()  # append-only rounds
    current: SowAuditView | None = None
    related_parties: RelatedPartyReview | None = None  # key individuals roll-up
    screening: ScreeningResult | None = None  # sanctions/PEP/watchlist screening
    scorecard: RiskScorecard | None = None  # risk-based scorecard + CDD tier
    source_of_funds: SourceOfFundsAssessment | None = None  # SoF (distinct from SoW)
    monitoring: MonitoringAssessment | None = None  # ongoing monitoring / periodic review
    requires_human_review: bool = True
    opened_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True, slots=True)
class SowSnapshot:
    """Immutable, versioned point-in-time view (what a checker approved)."""

    case_id: str
    version: int
    audit_view: SowAuditView
    approved_by: str
    sealed_at: datetime = field(default_factory=utcnow)


# --------------------------------------------------------------------------- #
# Related parties / key individuals (one system for companies AND people)
#
# Onboarding a company also means running CDD/SoW on its key individuals — UBOs,
# directors, controllers. A RelatedParty is a Subject (individual or intermediate
# entity) linked to a parent case with a role; in-scope parties are screened (CDD) and,
# when they are a source of the company's funds, get their own long-running SoW sub-case.
# Their outcomes roll up into the parent (soft escalation, checker can override).
# --------------------------------------------------------------------------- #
class PartyRole(enum.Enum):
    """How a related party is connected to the parent (company) subject."""

    BENEFICIAL_OWNER = "beneficial_owner"
    DIRECTOR = "director"
    CONTROLLER = "controller"
    AUTHORISED_SIGNATORY = "authorised_signatory"
    SHAREHOLDER = "shareholder"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class PartyScreening:
    """The CDD screening result for a related party (identity + PEP + adverse media)."""

    identity_verified: bool = False
    is_pep: bool = False
    sanctions_hit: bool = False
    adverse_media: Severity | None = None  # worst adverse-media severity, if any
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RelatedParty:
    """A key individual (or intermediate entity) linked to the parent case."""

    id: str
    subject: Subject
    role: PartyRole = PartyRole.BENEFICIAL_OWNER
    pct: float = 0.0  # beneficial ownership %, where applicable
    source_of_funds: bool = False  # if True, gets a full SoW sub-case
    citations: tuple[Citation, ...] = ()


@dataclass(frozen=True, slots=True)
class RelatedPartyOutcome:
    """The assessed outcome for one related party (CDD, optional SoW, roll-up flags)."""

    party: RelatedParty
    in_scope: bool
    screening: PartyScreening
    sow_case_id: str | None = None
    sow_status: CaseStatus | None = None
    sow_coverage_pct: float = 0.0
    cleared: bool = False
    escalates: bool = False  # contributes to parent escalation (only if in_scope)
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RelatedPartyReview:
    """The rolled-up review of a parent case's key individuals."""

    outcomes: tuple[RelatedPartyOutcome, ...] = ()
    escalated: bool = False  # soft signal — enhanced review, never auto-blocks
    summary: str = ""

    @property
    def in_scope(self) -> tuple[RelatedPartyOutcome, ...]:
        return tuple(o for o in self.outcomes if o.in_scope)

    @property
    def cleared_count(self) -> int:
        return sum(1 for o in self.in_scope if o.cleared)


# --------------------------------------------------------------------------- #
# Sanctions / PEP / watchlist screening
#
# A dedicated name-screening control: a deterministic matcher compares the subject
# (and key individuals) against versioned sanctions/PEP watchlists (OFAC SDN +
# Consolidated, UN, EU, UK HMT), producing alerts that an analyst dispositions
# (true/false positive) under four-eyes. Screening reads a *synced, point-in-time*
# list snapshot (version + hash) so every alert is reproducible and auditable.
# --------------------------------------------------------------------------- #
class ListSource(enum.Enum):
    """The watchlist a screening entry came from."""

    OFAC_SDN = "ofac_sdn"
    OFAC_CONSOLIDATED = "ofac_consolidated"
    UN = "un"
    EU = "eu"
    UK_HMT = "uk_hmt"
    PEP = "pep"


class HitStatus(enum.Enum):
    """Disposition of a screening alert (maker-checker)."""

    PENDING = "pending"
    TRUE_POSITIVE = "true_positive"
    FALSE_POSITIVE = "false_positive"


@dataclass(frozen=True, slots=True)
class WatchlistEntry:
    """One designated party on a sanctions/PEP watchlist (a synced snapshot row)."""

    uid: str  # stable id within the source, e.g. OFAC SDN entry number
    source: ListSource
    name: str
    entity_type: SubjectType = SubjectType.INDIVIDUAL
    aliases: tuple[str, ...] = ()
    dob: str | None = None  # ISO date or year, where published
    countries: tuple[str, ...] = ()
    programs: tuple[str, ...] = ()  # sanction programs / regimes
    remark: str = ""
    list_version: str = ""  # publish date / snapshot version the row came from


@dataclass(frozen=True, slots=True)
class ScreeningMatch:
    """A scored match between a screened name and a watchlist entry."""

    entry: WatchlistEntry
    score: float  # 0..1 combined name (+ DOB) similarity
    matched_name: str  # the entry name/alias that matched
    features: tuple[str, ...] = ()  # e.g. "name 0.95", "dob exact", "country SG"


@dataclass(frozen=True, slots=True)
class ScreeningAlert:
    """A single watchlist hit requiring disposition (true/false positive)."""

    id: str
    subject_id: str
    match: ScreeningMatch
    status: HitStatus = HitStatus.PENDING
    disposition_by: str = ""
    disposition_reason: str = ""
    created_at: datetime = field(default_factory=utcnow)

    @property
    def open(self) -> bool:
        """An alert is open until a checker marks it a false positive."""
        return self.status is not HitStatus.FALSE_POSITIVE


@dataclass(frozen=True, slots=True)
class ScreeningResult:
    """The screening outcome for a subject against a point-in-time list snapshot."""

    subject_id: str
    query_name: str
    lists_version: str = ""  # snapshot version/hash screened against (reproducibility)
    sources: tuple[ListSource, ...] = ()
    alerts: tuple[ScreeningAlert, ...] = ()
    screened_at: datetime = field(default_factory=utcnow)

    @property
    def open_alerts(self) -> tuple[ScreeningAlert, ...]:
        return tuple(a for a in self.alerts if a.open)

    @property
    def escalates(self) -> bool:
        """Any open alert (pending or confirmed true positive) escalates the case."""
        return bool(self.open_alerts)


# --------------------------------------------------------------------------- #
# Risk-based scorecard + CDD tiering (SDD / CDD / EDD)
#
# A deterministic, auditable customer-risk scorecard across the standard dimensions
# (customer type, geography/country, product, channel, plus financial-crime signals),
# producing an overall band and a CDD tier that drives the level of due diligence. It
# complements the LLM RiskRating with an explicit, replayable score an auditor can recompute.
# --------------------------------------------------------------------------- #
class CddTier(enum.Enum):
    """The due-diligence tier the risk-based approach assigns."""

    SDD = "sdd"  # simplified due diligence (low risk)
    CDD = "cdd"  # standard customer due diligence
    EDD = "edd"  # enhanced due diligence (high risk / PEP / sanctions / FATF)


@dataclass(frozen=True, slots=True)
class ScorecardFactor:
    """One weighted dimension of the risk scorecard."""

    name: str  # e.g. "geography", "customer_type", "pep_exposure", "sanctions"
    weight: float  # relative weight in [0, 1]
    score: float  # dimension risk in [0, 1]
    detail: str = ""

    @property
    def contribution(self) -> float:
        return self.weight * self.score


@dataclass(frozen=True, slots=True)
class RiskScorecard:
    """The deterministic risk-based scorecard + resulting band and CDD tier."""

    factors: tuple[ScorecardFactor, ...] = ()
    score: float = 0.0  # weighted total in [0, 1]
    band: RiskBand = RiskBand.LOW
    tier: CddTier = CddTier.CDD
    rationale: str = ""
    hard_signals: tuple[str, ...] = ()  # what forced EDD / a raised band, if anything


# --------------------------------------------------------------------------- #
# Source of Funds (SoF) — distinct from Source of Wealth (SoW)
#
# SoW explains the origin of the customer's *total accumulated wealth*; SoF explains the
# origin of the *specific funds* flowing into the account/relationship, and whether actual
# inflows match the expected-activity profile. A deterministic reconciliation of declared
# expected funding vs evidenced inflows, with computed gaps an analyst dispositions.
# --------------------------------------------------------------------------- #
class FundsOriginKind(enum.StrEnum):
    """How a tranche of incoming funds was generated (the SoF reference taxonomy)."""

    SALARY = "salary"
    BUSINESS_INCOME = "business_income"
    ASSET_SALE = "asset_sale"
    INVESTMENT_INCOME = "investment_income"
    LOAN = "loan"
    GIFT = "gift"
    INHERITANCE = "inheritance"
    OTHER = "other"


class FundsGapKind(enum.Enum):
    """A computed Source-of-Funds shortfall/anomaly (deterministic)."""

    UNEVIDENCED_INFLOW = "unevidenced_inflow"  # declared funding with no evidenced flow
    UNEXPECTED_INFLOW = "unexpected_inflow"  # evidenced flow with no declared origin
    MISSING_ORIGIN_DOC = "missing_origin_doc"  # declared origin lacks a corroborating doc
    ACTIVITY_MISMATCH = "activity_mismatch"  # evidenced inflow exceeds expected activity


@dataclass(frozen=True, slots=True)
class DeclaredFunds:
    """A funding source the client *expects* to bring into the relationship."""

    kind: str  # a FundsOriginKind value, or a deployment-specific extension
    description: str = ""
    expected_band: str = ""  # expected amount, e.g. "USD 1m-5m"


@dataclass(frozen=True, slots=True)
class FundsDeclaration:
    """The client's declared Source of Funds + expected-activity profile (at open)."""

    sources: tuple[DeclaredFunds, ...] = ()
    expected_inflow_band: str = ""  # total expected funding (initial / annual)
    expected_activity: str = ""  # narrative of expected account activity
    captured_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True, slots=True)
class FundsFlow:
    """An evidenced inflow of funds (e.g. a bank credit advice), corroborating SoF."""

    id: str
    kind: str  # a FundsOriginKind value, or a deployment-specific extension
    description: str = ""
    amount_band: str = ""
    value_date: str | None = None  # ISO date the funds landed
    citations: tuple[Citation, ...] = ()

    @property
    def corroborated(self) -> bool:
        return bool(self.citations)


@dataclass(frozen=True, slots=True)
class FundsLine:
    """One declared-vs-evidenced row of the SoF reconciliation."""

    kind: str  # the funds-origin kind this row reconciles
    declared_band: str = ""
    evidenced_band: str = ""
    coverage_pct: float = 0.0
    corroborated: bool = False
    citations: tuple[Citation, ...] = ()


@dataclass(frozen=True, slots=True)
class FundsGap:
    """A computed SoF gap, severity-ranked."""

    id: str
    kind: FundsGapKind
    severity: Severity
    summary: str
    related_kind: str | None = None  # the funds-origin kind implicated, if any
    detail: str = ""


@dataclass(frozen=True, slots=True)
class SourceOfFundsAssessment:
    """Deterministic Source-of-Funds reconciliation + gaps (distinct from SoW)."""

    subject_id: str
    declared_inflow_band: str = ""
    evidenced_inflow_band: str = ""
    coverage_pct: float = 0.0
    expected_activity: str = ""
    lines: tuple[FundsLine, ...] = ()
    gaps: tuple[FundsGap, ...] = ()
    consistency_notes: tuple[str, ...] = ()
    generated_at: datetime = field(default_factory=utcnow)

    @property
    def escalates(self) -> bool:
        return bool(self.gaps)


# --------------------------------------------------------------------------- #
# Ongoing monitoring / periodic review
#
# CDD is not one-and-done: an approved relationship must be kept current. Two forces
# drive a re-review — a risk-based *schedule* (EDD reviewed more often than SDD) and
# *event triggers* (a new sanctions hit, a PEP/ownership change, unusual activity).
# This is deterministic scheduling + trigger detection; a due/overdue/triggered case
# escalates softly to enhanced review and a checker disposes (never auto-blocks).
# --------------------------------------------------------------------------- #
class ReviewStatus(enum.Enum):
    """Where an approved case sits in its monitoring lifecycle."""

    CURRENT = "current"  # within cadence, no triggers
    DUE_SOON = "due_soon"  # periodic review falls within the look-ahead window
    OVERDUE = "overdue"  # periodic review date has passed
    TRIGGERED = "triggered"  # an event forces an out-of-cycle re-review


class ReviewTriggerKind(enum.Enum):
    """An event that forces an out-of-cycle re-review (deterministic)."""

    PERIODIC_DUE = "periodic_due"  # the risk-based schedule came due
    SANCTIONS_HIT = "sanctions_hit"  # a new/open sanctions or watchlist match
    PEP_STATUS = "pep_status"  # PEP exposure (new or ongoing)
    ADVERSE_MEDIA = "adverse_media"  # adverse media surfaced
    OWNERSHIP_CHANGE = "ownership_change"  # UBO / control structure changed
    UNUSUAL_ACTIVITY = "unusual_activity"  # activity diverges from expected (SoF)
    MATERIAL_CHANGE = "material_change"  # other material change in circumstances
    DOCUMENT_EXPIRY = "document_expiry"  # an identity / evidence document expired


@dataclass(frozen=True, slots=True)
class ReviewTrigger:
    """A single reason a case needs re-review, severity-ranked."""

    kind: ReviewTriggerKind
    severity: Severity
    summary: str
    detail: str = ""
    observed_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True, slots=True)
class MonitoringAssessment:
    """Deterministic ongoing-monitoring outcome: schedule + triggers for a case."""

    subject_id: str
    tier: CddTier = CddTier.CDD
    last_reviewed: str = ""  # ISO date of the last completed review
    next_review_due: str = ""  # ISO date the next periodic review is due
    review_status: ReviewStatus = ReviewStatus.CURRENT
    days_until_due: int = 0  # negative when overdue
    cadence_months: int = 0  # the risk-based cadence applied
    triggers: tuple[ReviewTrigger, ...] = ()
    notes: tuple[str, ...] = ()
    generated_at: datetime = field(default_factory=utcnow)

    @property
    def escalates(self) -> bool:
        """Any trigger, or an overdue periodic review, escalates to enhanced review."""
        return bool(self.triggers) or self.review_status is ReviewStatus.OVERDUE


# --------------------------------------------------------------------------- #
# Perpetual KYC (pKYC) — continuous, signal-driven re-assessment
#
# Periodic review above answers "when is this relationship next due?". Perpetual KYC
# answers "what has CHANGED since we last looked?" and re-scores the relationship the
# moment it changes. Three signal families feed it, each an external edge behind an
# existing port: sanctions/watchlist screening (SanctionsListProviderPort), adverse
# media (AdverseMediaPort) and corporate-registry ownership (CorporateRegistryPort).
#
# Every number below is computed by pure code (``domain/perpetual_kyc.py``): the model
# only narrates the outcome. A pKYC outcome is consequential, so it always carries
# ``requires_human_review`` and is routed to human-review-console (rule R8); it never auto-blocks.
# --------------------------------------------------------------------------- #
class SignalSource(enum.StrEnum):
    """Where a perpetual-KYC signal came from (the monitored external edge)."""

    SANCTIONS = "sanctions"
    ADVERSE_MEDIA = "adverse_media"
    REGISTRY = "registry"


class SignalChange(enum.StrEnum):
    """How a signal moved relative to the stored baseline (the delta vocabulary)."""

    NEW = "new"  # not present at the baseline: it drives the re-score up
    PERSISTING = "persisting"  # present at the baseline and still present
    CLEARED = "cleared"  # present at the baseline and no longer observed


class QueuePriority(enum.StrEnum):
    """Explainable review-queue priority (deterministic, never model-assigned)."""

    URGENT = "urgent"
    HIGH = "high"
    STANDARD = "standard"
    LOW = "low"


@dataclass(frozen=True, slots=True)
class MonitoringSignal:
    """One observed perpetual-KYC signal, keyed by a stable content fingerprint.

    ``key`` is derived deterministically from the source plus the signal's identifying
    content (see :func:`cdd_sow_research.domain.perpetual_kyc.signal_key`), so the same
    real-world fact produces the same key on every run and the baseline diff is stable.
    """

    key: str
    source: SignalSource
    change: SignalChange
    severity: Severity
    summary: str
    detail: str = ""
    citation: Citation | None = None
    source_version: str = ""  # snapshot/list version the signal was observed against
    observed_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True, slots=True)
class SignalUplift:
    """The deterministic score contribution of one signal (the audit line item)."""

    key: str
    source: SignalSource
    change: SignalChange
    severity: Severity
    uplift: float  # signed: positive raises the score, negative is clearance relief
    reason: str = ""


@dataclass(frozen=True, slots=True)
class BaselineSignal:
    """A signal as remembered by the baseline: its key plus what it weighed.

    The severity is carried (not just the key) so that when the signal later disappears
    the engine can relieve the score in proportion to what it originally added, instead of
    guessing. Without it, clearing a critical sanctions signal would be worth the same as
    clearing a low-severity media mention.
    """

    key: str
    source: SignalSource
    severity: Severity


@dataclass(frozen=True, slots=True)
class PerpetualKycBaseline:
    """The last accepted perpetual-KYC state for a subject (what "unchanged" means)."""

    subject_id: str
    tenant: str = ""
    as_of: str = ""  # ISO date the baseline was established
    signals: tuple[BaselineSignal, ...] = ()
    score: float = 0.0
    band: RiskBand = RiskBand.LOW
    tier: CddTier = CddTier.CDD
    acl: tuple[str, ...] = ()  # server-derived tags; never client-supplied

    @property
    def signal_keys(self) -> tuple[str, ...]:
        """The fingerprints the baseline knows about (the diff set)."""
        return tuple(s.key for s in self.signals)

    def signal_for(self, key: str) -> BaselineSignal | None:
        """The remembered signal behind a key, if the baseline carries it."""
        for signal in self.signals:
            if signal.key == key:
                return signal
        return None


@dataclass(frozen=True, slots=True)
class ReviewQueueItem:
    """An explainable place in the perpetual-KYC review queue.

    ``reasons`` are human-readable lines derived from the signals and the re-score, and
    ``citations`` carry the provenance behind them, so a reviewer opening the queue sees
    WHY this relationship surfaced without re-running anything.
    """

    id: str
    subject_id: str
    tenant: str = ""
    priority: QueuePriority = QueuePriority.STANDARD
    sla_due: str = ""  # ISO date the review is expected to be dispositioned by
    reasons: tuple[str, ...] = ()
    citations: tuple[Citation, ...] = ()
    requires_human_review: bool = True
    routed_to_hrz7: bool = False


@dataclass(frozen=True, slots=True)
class PerpetualKycAssessment:
    """The deterministic outcome of one perpetual-KYC run for a subject."""

    subject_id: str
    subject_name: str = ""
    tenant: str = ""
    as_of: str = ""  # ISO date the run was evaluated for (drives replayability)
    signals: tuple[MonitoringSignal, ...] = ()
    uplifts: tuple[SignalUplift, ...] = ()
    baseline_score: float = 0.0
    baseline_band: RiskBand = RiskBand.LOW
    score: float = 0.0
    score_delta: float = 0.0
    band: RiskBand = RiskBand.LOW
    tier: CddTier = CddTier.CDD
    rationale: str = ""
    monitoring: MonitoringAssessment | None = None
    queue_item: ReviewQueueItem | None = None
    narrative: str = ""  # LLM-written prose; carries no number the code did not compute
    lists_version: str = ""
    requires_human_review: bool = True
    acl: tuple[str, ...] = ()  # server-derived tags; never client-supplied
    generated_at: datetime = field(default_factory=utcnow)

    @property
    def new_signals(self) -> tuple[MonitoringSignal, ...]:
        return tuple(s for s in self.signals if s.change is SignalChange.NEW)

    @property
    def cleared_signals(self) -> tuple[MonitoringSignal, ...]:
        return tuple(s for s in self.signals if s.change is SignalChange.CLEARED)

    @property
    def material(self) -> bool:
        """True when something actually changed since the baseline."""
        return bool(self.new_signals or self.cleared_signals)

    @property
    def citations(self) -> tuple[Citation, ...]:
        """Every citation behind the signals, de-duplicated, in signal order."""
        seen: set[str] = set()
        out: list[Citation] = []
        for signal in self.signals:
            if signal.citation is None or signal.citation.source_id in seen:
                continue
            seen.add(signal.citation.source_id)
            out.append(signal.citation)
        return tuple(out)
