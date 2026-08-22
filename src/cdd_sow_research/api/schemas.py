"""Pydantic v2 request/response models for the B1 CDD + Source-of-Wealth API.

These schemas mirror the frozen domain dataclasses in
:mod:`cdd_sow_research.domain.models` one-for-one, so the HTTP boundary is a thin, typed
projection of the domain: the React/Next.js UI and the CLI consume exactly these shapes.
Each response model exposes a ``from_domain`` classmethod that builds itself from the
corresponding domain object (enums become their ``.value`` strings).

Nothing here imports Google Cloud, ADK, or any adapter: the API layer depends only on the
domain models, the ports, and the orchestration services, never on a concrete adapter.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from ..domain import models as m
from ..domain.case_bundle_service import RestoredBundle
from ..domain.serialization import to_jsonable
from .citation_ids import citation_identifier_from_url

# --------------------------------------------------------------------------- #
# Citation
# --------------------------------------------------------------------------- #


class CitationModel(BaseModel):
    """Source-grade provenance attached to a generated claim (mirror of Citation)."""

    source_id: str
    source_type: str
    title: str
    url: str = ""
    page: int | None = None
    snippet: str = ""
    score: float | None = None
    continuation_id: str = ""

    @classmethod
    def from_domain(
        cls,
        citation: m.Citation,
        continuation_ids: frozenset[str] = frozenset(),
    ) -> CitationModel:
        candidate = citation_identifier_from_url(
            citation.url,
            source_id=citation.source_id,
            page=citation.page,
        )
        return cls(
            **to_jsonable(citation),
            continuation_id=candidate if candidate in continuation_ids else "",
        )


# --------------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------------- #


class SubjectModel(BaseModel):
    id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    type: str = "individual"
    jurisdiction: str = ""
    dob_or_incorp: str | None = None

    def to_domain(self) -> m.Subject:
        return m.Subject(
            id=self.id,
            name=self.name,
            type=m.SubjectType(self.type),
            jurisdiction=self.jurisdiction,
            dob_or_incorp=self.dob_or_incorp,
        )


class DocumentModel(BaseModel):
    id: str = Field(..., min_length=1)
    doc_type: str = "other"
    uri: str = ""
    acl_tags: list[str] = Field(default_factory=list)

    def to_domain(self) -> m.KycDocument:
        return m.KycDocument(
            id=self.id,
            doc_type=m.DocType(self.doc_type),
            uri=self.uri,
            acl_tags=tuple(self.acl_tags),
        )


class CddRequest(BaseModel):
    """Inbound request to assess a full CDD case.

    Note: there is no ``actor`` field. The audit actor and entitlement principals are
    resolved server-side from the verified Principal (see ``api.security``); any
    client-supplied identity is ignored.
    """

    subject: SubjectModel
    # Bounded: a single request cannot enqueue an unbounded extraction/ingest fan-out.
    documents: list[DocumentModel] = Field(default_factory=list, max_length=50)

    def to_case_input(self) -> m.CaseInput:
        return m.CaseInput(
            subject=self.subject.to_domain(),
            documents=tuple(d.to_domain() for d in self.documents),
        )


class SubjectRequest(BaseModel):
    """Inbound request scoped to a subject (source-of-wealth, ownership).

    Identity is resolved server-side from the verified Principal, not from the body.
    """

    subject: SubjectModel


class AdverseMediaRequest(BaseModel):
    """Inbound request for an adverse-media scan.

    Identity is resolved server-side from the verified Principal, not from the body.
    """

    subject_name: str = Field(..., min_length=1)


class OpenSowCaseRequest(BaseModel):
    """Open a longitudinal SoW case. The case's tenant is set server-side from the
    verified Principal (never client-supplied), so a case cannot be planted in another
    tenant's ACL."""

    case_id: str = Field(..., min_length=1)
    subject: SubjectModel


class EvidenceItemModel(BaseModel):
    """One evidence item for a SoW-case round (minimal API projection of EvidenceItem)."""

    id: str = Field(..., min_length=1)
    document: DocumentModel
    provided_by: str = ""
    supports_kinds: list[str] = Field(default_factory=list)
    evidenced_band: str = ""
    idempotency_key: str = ""

    def to_domain(self) -> m.EvidenceItem:
        return m.EvidenceItem(
            id=self.id,
            document=self.document.to_domain(),
            provided_by=self.provided_by,
            supports_kinds=tuple(self.supports_kinds),
            evidenced_band=self.evidenced_band,
            idempotency_key=self.idempotency_key,
        )


class AddEvidenceRequest(BaseModel):
    items: list[EvidenceItemModel] = Field(default_factory=list)


class StoredDocumentModel(BaseModel):
    """A document held in custody for a case (mirror of StoredDocument, no bytes).

    ``uri`` is the API-RELATIVE path that serves the bytes back. It is relative because
    the same response is consumed standalone and embedded behind a portal path prefix,
    so the client resolves it against its own API base instead of the server guessing
    its public origin.
    """

    id: str
    filename: str = ""
    doc_type: str = "other"
    mime_type: str = ""
    size_bytes: int = 0
    pages: int = 0
    subject_id: str = ""
    uploaded_at: str = ""
    sha256: str = ""
    uri: str = ""

    @classmethod
    def from_domain(cls, record: m.StoredDocument) -> StoredDocumentModel:
        return cls(
            id=record.id,
            filename=record.filename,
            doc_type=record.doc_type.value,
            mime_type=record.mime_type,
            size_bytes=record.size_bytes,
            pages=record.pages,
            subject_id=record.subject_id,
            uploaded_at=record.uploaded_at,
            sha256=record.sha256,
            uri=record.uri,
        )


class DocumentListResponse(BaseModel):
    documents: list[StoredDocumentModel] = Field(default_factory=list)


class ReviewSowCaseRequest(BaseModel):
    approve: bool


# --------------------------------------------------------------------------- #
# Artifact responses
# --------------------------------------------------------------------------- #


class WealthSourceModel(BaseModel):
    kind: str
    description: str
    est_value_band: str = ""
    citations: list[CitationModel] = Field(default_factory=list)

    @classmethod
    def from_domain(
        cls,
        src: m.WealthSource,
        continuation_ids: frozenset[str] = frozenset(),
    ) -> WealthSourceModel:
        return cls(
            kind=str(src.kind),
            description=src.description,
            est_value_band=src.est_value_band,
            citations=[CitationModel.from_domain(c, continuation_ids) for c in src.citations],
        )


class SourceOfWealthResponse(BaseModel):
    subject_id: str
    narrative: str
    sources: list[WealthSourceModel] = Field(default_factory=list)
    citations: list[CitationModel] = Field(default_factory=list)
    confidence: float = 0.0
    requires_human_review: bool = True

    @classmethod
    def from_domain(
        cls,
        sow: m.SourceOfWealthNarrative,
        continuation_ids: frozenset[str] = frozenset(),
    ) -> SourceOfWealthResponse:
        return cls(
            subject_id=sow.subject_id,
            narrative=sow.narrative,
            sources=[WealthSourceModel.from_domain(s, continuation_ids) for s in sow.sources],
            citations=[CitationModel.from_domain(c, continuation_ids) for c in sow.citations],
            confidence=sow.confidence,
            requires_human_review=sow.requires_human_review,
        )


class RiskFactorModel(BaseModel):
    name: str
    weight: float
    present: bool
    detail: str = ""
    citations: list[CitationModel] = Field(default_factory=list)

    @classmethod
    def from_domain(
        cls,
        factor: m.RiskFactor,
        continuation_ids: frozenset[str] = frozenset(),
    ) -> RiskFactorModel:
        return cls(
            name=factor.name,
            weight=factor.weight,
            present=factor.present,
            detail=factor.detail,
            citations=[CitationModel.from_domain(c, continuation_ids) for c in factor.citations],
        )


class RiskRatingModel(BaseModel):
    band: str
    score: float
    factors: list[RiskFactorModel] = Field(default_factory=list)
    rationale: str = ""
    citations: list[CitationModel] = Field(default_factory=list)
    requires_human_review: bool = True

    @classmethod
    def from_domain(
        cls,
        rating: m.RiskRating,
        continuation_ids: frozenset[str] = frozenset(),
    ) -> RiskRatingModel:
        return cls(
            band=rating.band.value,
            score=rating.score,
            factors=[RiskFactorModel.from_domain(f, continuation_ids) for f in rating.factors],
            rationale=rating.rationale,
            citations=[CitationModel.from_domain(c, continuation_ids) for c in rating.citations],
            requires_human_review=rating.requires_human_review,
        )


class AdverseMediaModel(BaseModel):
    headline: str
    publisher: str
    url: str
    published_date: str | None = None
    category: str = "other"
    severity: str = "medium"
    snippet: str = ""
    citation: CitationModel | None = None

    @classmethod
    def from_domain(
        cls,
        finding: m.AdverseMediaFinding,
        continuation_ids: frozenset[str] = frozenset(),
    ) -> AdverseMediaModel:
        return cls(
            headline=finding.headline,
            publisher=finding.publisher,
            url=finding.url,
            published_date=finding.published_date,
            category=finding.category.value,
            severity=finding.severity.value,
            snippet=finding.snippet,
            citation=(
                CitationModel.from_domain(finding.citation, continuation_ids)
                if finding.citation is not None
                else None
            ),
        )


class BeneficialOwnerModel(BaseModel):
    name: str
    pct: float
    country: str = ""
    is_pep: bool = False
    citations: list[CitationModel] = Field(default_factory=list)

    @classmethod
    def from_domain(
        cls,
        owner: m.BeneficialOwner,
        continuation_ids: frozenset[str] = frozenset(),
    ) -> BeneficialOwnerModel:
        return cls(
            name=owner.name,
            pct=owner.pct,
            country=owner.country,
            is_pep=owner.is_pep,
            citations=[CitationModel.from_domain(c, continuation_ids) for c in owner.citations],
        )


class OwnershipSummaryModel(BaseModel):
    root_entity: str
    owners: list[BeneficialOwnerModel] = Field(default_factory=list)
    tree: dict[str, Any] | None = None
    citations: list[CitationModel] = Field(default_factory=list)

    @classmethod
    def from_domain(
        cls,
        summary: m.OwnershipSummary,
        continuation_ids: frozenset[str] = frozenset(),
    ) -> OwnershipSummaryModel:
        return cls(
            root_entity=summary.root_entity,
            owners=[BeneficialOwnerModel.from_domain(o, continuation_ids) for o in summary.owners],
            tree=to_jsonable(summary.tree) if summary.tree is not None else None,
            citations=[CitationModel.from_domain(c, continuation_ids) for c in summary.citations],
        )


class WatchlistEntryModel(BaseModel):
    """The watchlist row a screening alert matched (mirror of WatchlistEntry)."""

    uid: str
    source: str
    name: str
    entity_type: str = "individual"
    aliases: list[str] = Field(default_factory=list)
    dob: str | None = None
    countries: list[str] = Field(default_factory=list)
    programs: list[str] = Field(default_factory=list)

    @classmethod
    def from_domain(cls, entry: m.WatchlistEntry) -> WatchlistEntryModel:
        return cls(
            uid=entry.uid,
            source=entry.source.value,
            name=entry.name,
            entity_type=entry.entity_type.value,
            aliases=list(entry.aliases),
            dob=entry.dob,
            countries=list(entry.countries),
            programs=list(entry.programs),
        )


class ScreeningAlertModel(BaseModel):
    """One watchlist hit awaiting disposition (mirror of ScreeningAlert)."""

    id: str
    status: str
    score: float
    matched_name: str
    features: list[str] = Field(default_factory=list)
    entry: WatchlistEntryModel

    @classmethod
    def from_domain(cls, alert: m.ScreeningAlert) -> ScreeningAlertModel:
        return cls(
            id=alert.id,
            status=alert.status.value,
            score=alert.match.score,
            matched_name=alert.match.matched_name,
            features=list(alert.match.features),
            entry=WatchlistEntryModel.from_domain(alert.match.entry),
        )


class ScreeningResultModel(BaseModel):
    """The point-in-time screening outcome for the subject (mirror of ScreeningResult)."""

    query_name: str
    lists_version: str = ""
    sources: list[str] = Field(default_factory=list)
    alerts: list[ScreeningAlertModel] = Field(default_factory=list)
    screened_at: str = ""

    @classmethod
    def from_domain(cls, result: m.ScreeningResult) -> ScreeningResultModel:
        return cls(
            query_name=result.query_name,
            lists_version=result.lists_version,
            sources=[s.value for s in result.sources],
            alerts=[ScreeningAlertModel.from_domain(a) for a in result.alerts],
            screened_at=result.screened_at.isoformat(),
        )


class AdverseMediaScreeningModel(BaseModel):
    """An adverse-media screen (mirror of AdverseMediaScreening).

    Its presence on a response says a screen ran. Its ``findings`` say what the screen
    returned. A caller that receives ``null`` was not screened, which is a different
    statement from a screen that returned nothing.
    """

    subject_name: str
    findings: list[AdverseMediaModel] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    searched_at: str = ""

    @classmethod
    def from_domain(
        cls,
        screening: m.AdverseMediaScreening,
        continuation_ids: frozenset[str] = frozenset(),
    ) -> AdverseMediaScreeningModel:
        return cls(
            subject_name=screening.subject_name,
            findings=[
                AdverseMediaModel.from_domain(f, continuation_ids) for f in screening.findings
            ],
            sources=list(screening.sources),
            searched_at=screening.searched_at.isoformat(),
        )


class CddCaseResponse(BaseModel):
    """The full CDD dossier (mirror of CDDCase)."""

    id: str
    subject: SubjectModel
    sow: SourceOfWealthResponse
    rating: RiskRatingModel
    # None = no adverse-media screen ran; [] findings = searched and clear.
    adverse_media: AdverseMediaScreeningModel | None = None
    ownership: OwnershipSummaryModel | None = None
    # None = the case was not screened; [] alerts = screened and clear.
    screening: ScreeningResultModel | None = None
    requires_human_review: bool = True
    generated_at: str = ""

    @classmethod
    def from_domain(
        cls,
        case: m.CDDCase,
        continuation_ids: frozenset[str] = frozenset(),
    ) -> CddCaseResponse:
        return cls(
            id=case.id,
            subject=SubjectModel(
                id=case.subject.id,
                name=case.subject.name,
                type=case.subject.type.value,
                jurisdiction=case.subject.jurisdiction,
                dob_or_incorp=case.subject.dob_or_incorp,
            ),
            sow=SourceOfWealthResponse.from_domain(case.sow, continuation_ids),
            rating=RiskRatingModel.from_domain(case.rating, continuation_ids),
            adverse_media=(
                AdverseMediaScreeningModel.from_domain(case.adverse_media, continuation_ids)
                if case.adverse_media is not None
                else None
            ),
            ownership=(
                OwnershipSummaryModel.from_domain(case.ownership, continuation_ids)
                if case.ownership is not None
                else None
            ),
            screening=(
                ScreeningResultModel.from_domain(case.screening)
                if case.screening is not None
                else None
            ),
            requires_human_review=case.requires_human_review,
            generated_at=case.generated_at.isoformat(),
        )


class PortableDossierArtifact(BaseModel):
    """Open, integrity-protected dossier envelope for export and reload."""

    schema_version: Literal["cdd-dossier/v1"] = "cdd-dossier/v1"
    sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    exported_at: str = Field(min_length=1)
    dossier: CddCaseResponse


class CaseBundleRestoreResponse(BaseModel):
    """What a reloaded case bundle put back, and under whose terms.

    ``dossier`` is returned as the raw JSON object the bundle carried rather than a
    parsed :class:`CddCaseResponse`: a bundle from an older build may hold fields this
    one does not model, and silently dropping them on reload would make the archive a
    lossy format. The caller decides what to do with the extra keys.
    """

    schema_version: str
    case_id: str
    exported_at: str
    manifest_sha256: str
    dossier: dict[str, Any]
    documents: list[StoredDocumentModel] = Field(default_factory=list)
    retained_existing: list[str] = Field(default_factory=list)

    @classmethod
    def from_domain(cls, restored: RestoredBundle) -> CaseBundleRestoreResponse:
        return cls(
            schema_version=restored.manifest.schema_version,
            case_id=restored.manifest.case_id,
            exported_at=restored.manifest.exported_at,
            manifest_sha256=restored.manifest_sha256,
            dossier=restored.dossier,
            documents=[StoredDocumentModel.from_domain(r) for r in restored.documents],
            retained_existing=list(restored.retained_existing),
        )


class AdverseMediaResponse(BaseModel):
    subject_name: str
    # None = no screen ran (no reachable backend, or the port refused); a screening with
    # [] findings = searched and clear. A caller cannot tell those apart from a bare list.
    screening: AdverseMediaScreeningModel | None = None

    @classmethod
    def from_domain(
        cls, subject_name: str, screening: m.AdverseMediaScreening | None
    ) -> AdverseMediaResponse:
        return cls(
            subject_name=subject_name,
            screening=(
                AdverseMediaScreeningModel.from_domain(screening) if screening is not None else None
            ),
        )


# --------------------------------------------------------------------------- #
# Perpetual KYC
# --------------------------------------------------------------------------- #


class PerpetualKycRequest(BaseModel):
    """Run one perpetual-KYC cycle for a subject.

    There is no ``actor`` and no ``tenant`` field: both are resolved server-side from the
    verified Principal, and the subject's tenant is stamped from it, so a caller can never
    write a monitoring record into another tenant's ACL. ``as_of`` makes a run replayable
    (an auditor can recompute the exact assessment); it defaults to today.
    """

    subject: SubjectModel
    as_of: str = ""  # ISO date; empty means today
    last_reviewed: str = ""  # ISO date of the last completed periodic review


class MonitoringSignalModel(BaseModel):
    """One observed perpetual-KYC signal and how it moved against the baseline."""

    key: str
    source: str
    change: str
    severity: str
    summary: str
    detail: str = ""
    citation: CitationModel | None = None
    source_version: str = ""
    observed_at: str = ""

    @classmethod
    def from_domain(cls, signal: m.MonitoringSignal) -> MonitoringSignalModel:
        return cls(
            key=signal.key,
            source=signal.source.value,
            change=signal.change.value,
            severity=signal.severity.value,
            summary=signal.summary,
            detail=signal.detail,
            citation=(
                CitationModel.from_domain(signal.citation) if signal.citation is not None else None
            ),
            source_version=signal.source_version,
            observed_at=signal.observed_at.isoformat(),
        )


class SignalUpliftModel(BaseModel):
    """The deterministic score contribution of one signal (the audit line item)."""

    key: str
    source: str
    change: str
    severity: str
    uplift: float
    reason: str = ""

    @classmethod
    def from_domain(cls, uplift: m.SignalUplift) -> SignalUpliftModel:
        return cls(
            key=uplift.key,
            source=uplift.source.value,
            change=uplift.change.value,
            severity=uplift.severity.value,
            uplift=uplift.uplift,
            reason=uplift.reason,
        )


class ReviewQueueItemModel(BaseModel):
    """The explainable review-queue entry a checker opens."""

    id: str
    subject_id: str
    tenant: str = ""
    priority: str = "standard"
    sla_due: str = ""
    reasons: list[str] = Field(default_factory=list)
    citations: list[CitationModel] = Field(default_factory=list)
    requires_human_review: bool = True
    routed_to_hrz7: bool = False

    @classmethod
    def from_domain(cls, item: m.ReviewQueueItem) -> ReviewQueueItemModel:
        return cls(
            id=item.id,
            subject_id=item.subject_id,
            tenant=item.tenant,
            priority=item.priority.value,
            sla_due=item.sla_due,
            reasons=list(item.reasons),
            citations=[CitationModel.from_domain(c) for c in item.citations],
            requires_human_review=item.requires_human_review,
            routed_to_hrz7=item.routed_to_hrz7,
        )


class PerpetualKycResponse(BaseModel):
    """One perpetual-KYC assessment: what changed, the re-score, and the queue place."""

    subject_id: str
    subject_name: str = ""
    tenant: str = ""
    as_of: str = ""
    signals: list[MonitoringSignalModel] = Field(default_factory=list)
    uplifts: list[SignalUpliftModel] = Field(default_factory=list)
    baseline_score: float = 0.0
    baseline_band: str = "low"
    score: float = 0.0
    score_delta: float = 0.0
    band: str = "low"
    tier: str = "cdd"
    rationale: str = ""
    narrative: str = ""
    lists_version: str = ""
    requires_human_review: bool = True
    queue_item: ReviewQueueItemModel | None = None
    generated_at: str = ""

    @classmethod
    def from_domain(cls, assessment: m.PerpetualKycAssessment) -> PerpetualKycResponse:
        return cls(
            subject_id=assessment.subject_id,
            subject_name=assessment.subject_name,
            tenant=assessment.tenant,
            as_of=assessment.as_of,
            signals=[MonitoringSignalModel.from_domain(s) for s in assessment.signals],
            uplifts=[SignalUpliftModel.from_domain(u) for u in assessment.uplifts],
            baseline_score=assessment.baseline_score,
            baseline_band=assessment.baseline_band.value,
            score=assessment.score,
            score_delta=assessment.score_delta,
            band=assessment.band.value,
            tier=assessment.tier.value,
            rationale=assessment.rationale,
            narrative=assessment.narrative,
            lists_version=assessment.lists_version,
            requires_human_review=assessment.requires_human_review,
            queue_item=(
                ReviewQueueItemModel.from_domain(assessment.queue_item)
                if assessment.queue_item is not None
                else None
            ),
            generated_at=assessment.generated_at.isoformat(),
        )


class PerpetualKycQueueResponse(BaseModel):
    """The caller's tenant-scoped perpetual-KYC review queue, most urgent first."""

    items: list[PerpetualKycResponse] = Field(default_factory=list)

    @classmethod
    def from_domain(
        cls, assessments: tuple[m.PerpetualKycAssessment, ...]
    ) -> PerpetualKycQueueResponse:
        return cls(items=[PerpetualKycResponse.from_domain(a) for a in assessments])


# --------------------------------------------------------------------------- #
# UBO graph
#
# FROZEN CONTRACT. ``UboGraphResponse`` is the shape a downstream consumer (Doc1's A2A
# ``resolve_ubo_graph`` skill, and G2 when it is built) reads, and it is versioned by the
# agent card rather than by an unannounced edit here. Fields may be ADDED; a field may not
# be renamed, retyped or removed without a card version bump. See
# docs/ubo-graph-contract.md.
# --------------------------------------------------------------------------- #


class UboGraphRequest(BaseModel):
    """Resolve the cross-jurisdiction ownership structure behind an entity subject.

    There is no ``actor`` and no ``tenant`` field: both are resolved server-side from the
    verified Principal, and the subject's tenant is stamped from it, so a caller can never
    route a resolution under another tenant's ACL. ``as_of`` makes a run replayable (an
    auditor can recompute the exact resolution); it defaults to today.
    """

    subject: SubjectModel
    as_of: str = ""  # ISO date; empty means today


class OwnershipGraphNodeModel(BaseModel):
    """One party in the structure, as a registry recorded it."""

    id: str
    name: str
    kind: str = "unknown"
    jurisdiction: str = ""
    registered_address: str = ""
    incorporation_date: str = ""
    status: str = ""
    is_pep: bool = False
    depth: int = 0
    citations: list[CitationModel] = Field(default_factory=list)

    @classmethod
    def from_domain(cls, node: m.OwnershipGraphNode) -> OwnershipGraphNodeModel:
        return cls(
            id=node.id,
            name=node.name,
            kind=node.kind.value,
            jurisdiction=node.jurisdiction,
            registered_address=node.registered_address,
            incorporation_date=node.incorporation_date,
            status=node.status,
            is_pep=node.is_pep,
            depth=node.depth,
            citations=[CitationModel.from_domain(c) for c in node.citations],
        )


class OwnershipEdgeModel(BaseModel):
    """One recorded connection, directed from the owner to the party owned."""

    source_id: str
    target_id: str
    kind: str = "shareholding"
    pct: float = 0.0
    as_of: str = ""
    citations: list[CitationModel] = Field(default_factory=list)

    @classmethod
    def from_domain(cls, edge: m.OwnershipEdge) -> OwnershipEdgeModel:
        return cls(
            source_id=edge.source_id,
            target_id=edge.target_id,
            kind=edge.kind.value,
            pct=edge.pct,
            as_of=edge.as_of,
            citations=[CitationModel.from_domain(c) for c in edge.citations],
        )


class OwnershipGraphResponse(BaseModel):
    """The walked structure alone: layers, edges and citations, and no verdict."""

    root_id: str
    root_name: str = ""
    nodes: list[OwnershipGraphNodeModel] = Field(default_factory=list)
    edges: list[OwnershipEdgeModel] = Field(default_factory=list)
    depth: int = 0
    truncated: bool = False
    unresolved_ids: list[str] = Field(default_factory=list)
    jurisdictions: list[str] = Field(default_factory=list)
    as_of: str = ""

    @classmethod
    def from_domain(cls, graph: m.OwnershipGraph) -> OwnershipGraphResponse:
        return cls(
            root_id=graph.root_id,
            root_name=graph.root_name,
            nodes=[OwnershipGraphNodeModel.from_domain(n) for n in graph.nodes],
            edges=[OwnershipEdgeModel.from_domain(e) for e in graph.edges],
            depth=graph.depth,
            truncated=graph.truncated,
            unresolved_ids=list(graph.unresolved_ids),
            jurisdictions=list(graph.jurisdictions),
            as_of=graph.as_of,
        )


class OwnershipPathStepModel(BaseModel):
    """One hop of a path, carrying the percentage that hop contributes."""

    source_id: str
    target_id: str
    source_name: str = ""
    target_name: str = ""
    kind: str = "shareholding"
    pct: float = 0.0

    @classmethod
    def from_domain(cls, step: m.OwnershipPathStep) -> OwnershipPathStepModel:
        return cls(
            source_id=step.source_id,
            target_id=step.target_id,
            source_name=step.source_name,
            target_name=step.target_name,
            kind=step.kind.value,
            pct=step.pct,
        )


class OwnershipPathModel(BaseModel):
    """One simple path from an owner down to the subject, with its own arithmetic."""

    steps: list[OwnershipPathStepModel] = Field(default_factory=list)
    product_pct: float = 0.0
    arithmetic: str = ""  # the multiplication, rendered: "60.00% x 50.00% = 30.0000%"
    citations: list[CitationModel] = Field(default_factory=list)

    @classmethod
    def from_domain(cls, path: m.OwnershipPath) -> OwnershipPathModel:
        return cls(
            steps=[OwnershipPathStepModel.from_domain(s) for s in path.steps],
            product_pct=path.product_pct,
            arithmetic=path.arithmetic,
            citations=[CitationModel.from_domain(c) for c in path.citations],
        )


class UboFindingModel(BaseModel):
    """One candidate beneficial owner or controller, with the paths behind it."""

    node_id: str
    name: str
    kind: str = "unknown"
    jurisdiction: str = ""
    is_pep: bool = False
    effective_pct: float = 0.0
    paths: list[OwnershipPathModel] = Field(default_factory=list)
    control_basis: str = "none"
    control_reason: str = ""
    meets_threshold: bool = False
    citations: list[CitationModel] = Field(default_factory=list)

    @classmethod
    def from_domain(cls, finding: m.UboFinding) -> UboFindingModel:
        return cls(
            node_id=finding.node_id,
            name=finding.name,
            kind=finding.kind.value,
            jurisdiction=finding.jurisdiction,
            is_pep=finding.is_pep,
            effective_pct=finding.effective_pct,
            paths=[OwnershipPathModel.from_domain(p) for p in finding.paths],
            control_basis=finding.control_basis.value,
            control_reason=finding.control_reason,
            meets_threshold=finding.meets_threshold,
            citations=[CitationModel.from_domain(c) for c in finding.citations],
        )


class OwnershipFlagModel(BaseModel):
    """A deterministic INDICATOR with its reason. Never a conclusion."""

    kind: str
    severity: str = "medium"
    node_id: str = ""
    node_name: str = ""
    summary: str = ""
    detail: str = ""
    citations: list[CitationModel] = Field(default_factory=list)

    @classmethod
    def from_domain(cls, flag: m.OwnershipFlag) -> OwnershipFlagModel:
        return cls(
            kind=flag.kind.value,
            severity=flag.severity.value,
            node_id=flag.node_id,
            node_name=flag.node_name,
            summary=flag.summary,
            detail=flag.detail,
            citations=[CitationModel.from_domain(c) for c in flag.citations],
        )


class UboGraphResponse(BaseModel):
    """One UBO resolution: the structure, who owns it, on what basis, and the flags."""

    subject_id: str
    subject_name: str = ""
    tenant: str = ""
    as_of: str = ""
    graph: OwnershipGraphResponse | None = None
    findings: list[UboFindingModel] = Field(default_factory=list)
    beneficial_owners: list[UboFindingModel] = Field(default_factory=list)
    control_basis: str = "none"
    control_rationale: str = ""
    flags: list[OwnershipFlagModel] = Field(default_factory=list)
    opacity_score: float = 0.0
    ownership_threshold_pct: float = 25.0
    rationale: str = ""
    narrative: str = ""
    requires_human_review: bool = True
    routed_to_hrz7: bool = False
    generated_at: str = ""

    @classmethod
    def from_domain(cls, resolution: m.UboResolution) -> UboGraphResponse:
        return cls(
            subject_id=resolution.subject_id,
            subject_name=resolution.subject_name,
            tenant=resolution.tenant,
            as_of=resolution.as_of,
            graph=(
                OwnershipGraphResponse.from_domain(resolution.graph)
                if resolution.graph is not None
                else None
            ),
            findings=[UboFindingModel.from_domain(f) for f in resolution.findings],
            beneficial_owners=[
                UboFindingModel.from_domain(f) for f in resolution.beneficial_owners
            ],
            control_basis=resolution.control_basis.value,
            control_rationale=resolution.control_rationale,
            flags=[OwnershipFlagModel.from_domain(f) for f in resolution.flags],
            opacity_score=resolution.opacity_score,
            ownership_threshold_pct=resolution.ownership_threshold_pct,
            rationale=resolution.rationale,
            narrative=resolution.narrative,
            requires_human_review=resolution.requires_human_review,
            routed_to_hrz7=resolution.routed_to_hrz7,
            generated_at=resolution.generated_at.isoformat(),
        )


# --------------------------------------------------------------------------- #
# Health & governance
# --------------------------------------------------------------------------- #


class HealthResponse(BaseModel):
    status: str = "ok"
    profile: str = "local"
    region: str = "us-central1"
    mode: str = "application"  # Deprecated control-ownership compatibility state.
    identity_mode: str = "local-persona"
    channel_mode: str = "standalone"
    manifest_version: str = "not-configured"
    deployment_manifest_id: str = "not-configured"
    build_id: str = "not-configured"
    manifest_sha256: str = ""
    configuration_hash: str = ""
    demo_only: bool = True
    production_ready: bool = False


class CapabilityModel(BaseModel):
    name: str
    available: bool
    mode: str
    assurance: str
    provider: str = ""
    reason: str = ""
    required_for_production: bool = False


class CapabilityManifestModel(BaseModel):
    service: str
    profile: str
    region: str
    capabilities: list[CapabilityModel]
    schema_version: str = "capability-manifest/v1"
    portable_core: bool = True
    demo_only: bool = False
    production_ready: bool = False


class AgentSkillModel(BaseModel):
    id: str
    name: str
    description: str


class AgentCardModel(BaseModel):
    """A2A AgentCard served at /.well-known/agent-card.json (mirror of AgentCard)."""

    name: str
    description: str
    url: str
    version: str
    provider: str = "cdd-sow-research"
    skills: list[AgentSkillModel] = Field(default_factory=list)

    @classmethod
    def from_domain(cls, card: m.AgentCard) -> AgentCardModel:
        return cls(**to_jsonable(card))
