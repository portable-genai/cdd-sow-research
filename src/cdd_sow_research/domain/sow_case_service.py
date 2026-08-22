"""SowCaseService — orchestrates the long-running, auditable SoW workflow.

This is the longitudinal counterpart of :class:`~cdd_sow_research.domain.cdd_service` for
Source of Wealth: instead of one synchronous pass it drives a case that lives for weeks
across append-only iterations (see ``docs/sow-longitudinal-audit-design.md``):

    open() -> add_evidence() -> analyze() -> (RFI back to client) -> add_evidence() -> ...
            -> analyze() -> review() -> sealed snapshot

It talks only to ports and pure services. The consequential math (reconciliation, gaps)
is delegated to the deterministic :class:`GapAnalysisService`; the narrative is built by
an injected ``narrator`` (an LLM-backed callable in production, a deterministic default
here so the flow runs and tests offline). Every state transition is guarded by
:class:`CaseTransitionPolicy` and, when an audit sink is wired, recorded.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from ..ports.case_store import CaseStorePort
from ..ports.observability import AuditSinkPort
from .case_policy import CaseTransitionPolicy
from .errors import FourEyesError, InvalidTransitionError
from .gap_analysis import GapAnalysisService
from .models import (
    AuditEvent,
    CaseStatus,
    Decision,
    EvidenceItem,
    MonitoringAssessment,
    RelatedPartyReview,
    RiskScorecard,
    ScreeningResult,
    SourceGroup,
    SourceOfFundsAssessment,
    SourceOfWealthNarrative,
    SowAuditView,
    SowCase,
    SowIteration,
    SowSnapshot,
    Subject,
    WealthDeclaration,
    WealthReconciliation,
    WealthSource,
    utcnow,
)
from .rfi_drafting import RfiDraftingService

#: A narrator turns the computed groups + reconciliation into a cited narrative.
Narrator = Callable[
    [SowCase, tuple[SourceGroup, ...], WealthReconciliation], SourceOfWealthNarrative
]


def _default_narrator(
    case: SowCase,
    groups: tuple[SourceGroup, ...],
    reconciliation: WealthReconciliation,
) -> SourceOfWealthNarrative:
    """Deterministic, offline narrator: compose a grounded narrative from the figures.

    Production swaps in an LLM-backed narrator; this default keeps the flow runnable and
    its output predictable for tests and the demo. It never invents a figure the engine
    did not compute, so it cannot contradict the reconciliation.
    """

    def _describe(g: SourceGroup) -> str:
        label = str(g.kind).replace("_", " ").title()
        text = f"{label} evidenced at {g.evidenced_band or 'no figure yet'}"
        return text + (f" (declared {g.declared_band})" if g.declared_band else "")

    sources = tuple(
        WealthSource(
            kind=g.kind,
            description=_describe(g),
            est_value_band=g.evidenced_band,
            citations=g.citations,
        )
        for g in groups
    )
    corroborated = [g for g in groups if g.corroborated]
    pct = round(reconciliation.coverage_pct * 100)
    if corroborated:
        lead = (
            f"The subject's wealth is attributed to {len(groups)} declared source(s); "
            f"{len(corroborated)} carry corroborating evidence. "
            f"Evidenced {reconciliation.evidenced_total_band or 'nothing yet'} against a declared "
            f"{reconciliation.declared_total_band or 'undisclosed'} net worth ({pct}% coverage)."
        )
    else:
        lead = (
            "No declared source carries corroborating evidence yet; this narrative is "
            "provisional pending the documents requested from the client."
        )
    citations = tuple({(c.source_id, c.page): c for g in groups for c in g.citations}.values())
    return SourceOfWealthNarrative(
        subject_id=case.subject.id,
        narrative=lead,
        sources=sources,
        citations=citations,
        confidence=round(reconciliation.coverage_pct, 2),
        requires_human_review=True,
    )


@dataclass(frozen=True, slots=True)
class SowCaseService:
    """Drive a long-running SoW case across iterations. Talks only to ports/services."""

    store: CaseStorePort
    gap_analysis: GapAnalysisService = field(default_factory=GapAnalysisService)
    policy: CaseTransitionPolicy = field(default_factory=CaseTransitionPolicy)
    narrator: Narrator = _default_narrator
    rfi_drafter: RfiDraftingService | None = None  # lazily defaulted
    audit: AuditSinkPort | None = None  # optional WORM recorder

    @classmethod
    def from_policy(cls, store: CaseStorePort, risk_policy, **kwargs) -> SowCaseService:
        """Build with the gap engine parameterised by a bank-owned :class:`RiskPolicy`.

        Threads ``policy.gap`` (tolerances + mandatory-doc tables) from settings into the
        deterministic reconciliation/gap engine, so the SoW-case path is settings-driven
        rather than using the hardcoded defaults.
        """
        return cls(
            store=store, gap_analysis=GapAnalysisService.from_policy(risk_policy.gap), **kwargs
        )

    # ------------------------------------------------------------------ #
    def open(
        self,
        case_id: str,
        subject: Subject,
        declaration: WealthDeclaration | None,
        actor: str,
    ) -> SowCase:
        """Open a new case in DRAFT and persist it (version 0)."""
        case = SowCase(
            id=case_id,
            subject=subject,
            status=CaseStatus.DRAFT,
            version=0,
            declaration=declaration,
        )
        stored = self.store.open(case)
        self._record(stored, "sow_case_open", Decision.ALLOWED, actor)
        return stored

    def add_evidence(
        self,
        case_id: str,
        principals: tuple[str, ...],
        items: Sequence[EvidenceItem],
        actor: str,
    ) -> SowCase:
        """Append a round of evidence to the open iteration (idempotent intake)."""
        case = self.store.load(case_id, principals)
        target = CaseStatus.GATHERING
        if case.status is not CaseStatus.GATHERING:
            self._require(case.status, target)

        round_no = len(case.iterations)
        existing_keys = {it.idempotency_key for it in case.ledger if it.idempotency_key}
        new_items = [
            self._stamp(it, round_no)
            for it in items
            if not (it.idempotency_key and it.idempotency_key in existing_keys)
        ]
        updated = self._replace(
            case,
            status=target,
            ledger=case.ledger + tuple(new_items),
        )
        saved = self.store.save(updated, expected_version=case.version)
        self._record(saved, "sow_case_add_evidence", Decision.ALLOWED, actor)
        return saved

    def analyze(
        self,
        case_id: str,
        principals: tuple[str, ...],
        actor: str,
        as_of: datetime | None = None,
    ) -> SowCase:
        """Run one analysis round: reconcile, find gaps, draft RFIs, advance state."""
        case = self.store.load(case_id, principals)
        self._require(case.status, CaseStatus.ANALYSING)
        analysing = self._replace(case, status=CaseStatus.ANALYSING)

        result = self.gap_analysis.analyze(analysing, as_of=as_of)
        narrative = self.narrator(analysing, result.groups, result.reconciliation)
        rfis = self._drafter().draft(result.gaps)
        view = SowAuditView(
            subject_id=case.subject.id,
            narrative=narrative,
            groups=result.groups,
            reconciliation=result.reconciliation,
            gaps=result.gaps,
            suggested_changes=(),
            rfis=rfis,
            related_parties=case.related_parties,
            screening=case.screening,
            scorecard=case.scorecard,
            source_of_funds=case.source_of_funds,
            monitoring=case.monitoring,
        )

        next_status = CaseStatus.RFI_PENDING if result.gaps else CaseStatus.READY_FOR_REVIEW
        self._require(CaseStatus.ANALYSING, next_status)
        iteration = SowIteration(
            no=len(case.iterations),
            added_evidence_ids=tuple(
                it.id for it in case.ledger if it.iteration_no == len(case.iterations)
            ),
            audit_view=view,
            rfi_ids=tuple(r.id for r in rfis),
            actor=actor,
        )
        updated = self._replace(
            case,
            status=next_status,
            current=view,
            iterations=case.iterations + (iteration,),
        )
        saved = self.store.save(updated, expected_version=case.version)
        decision = Decision.ESCALATED if result.gaps else Decision.ALLOWED
        self._record(saved, "sow_case_analyze", decision, actor)
        return saved

    def attach_related_parties(
        self,
        case_id: str,
        principals: tuple[str, ...],
        review: RelatedPartyReview,
        actor: str,
    ) -> SowCase:
        """Attach the key-individuals roll-up to the case (soft escalation).

        Stores the review on the case and on the current audit view so the UI and audit
        record carry it. Escalation raises the review bar but never blocks — the checker
        still disposes (P-06); approval stays governed by four-eyes in ``review()``.
        """
        from dataclasses import replace

        case = self.store.load(case_id, principals)
        current = replace(case.current, related_parties=review) if case.current else None
        updated = self._replace(case, related_parties=review, current=current)
        saved = self.store.save(updated, expected_version=case.version)
        decision = Decision.ESCALATED if review.escalated else Decision.ALLOWED
        self._record(saved, "sow_case_related_parties", decision, actor)
        return saved

    def attach_screening(
        self,
        case_id: str,
        principals: tuple[str, ...],
        result: ScreeningResult,
        actor: str,
    ) -> SowCase:
        """Attach a sanctions/PEP/watchlist screening result (soft escalation).

        Stored on the case and current audit view so the UI and audit record carry it.
        Any open alert escalates to enhanced review; the checker still disposes (P-06).
        """
        from dataclasses import replace

        case = self.store.load(case_id, principals)
        current = replace(case.current, screening=result) if case.current else None
        updated = self._replace(case, screening=result, current=current)
        saved = self.store.save(updated, expected_version=case.version)
        decision = Decision.ESCALATED if result.escalates else Decision.ALLOWED
        self._record(saved, "sow_case_screening", decision, actor)
        return saved

    def attach_scorecard(
        self,
        case_id: str,
        principals: tuple[str, ...],
        scorecard: RiskScorecard,
        actor: str,
    ) -> SowCase:
        """Attach the risk-based scorecard + CDD tier (EDD escalates; checker disposes)."""
        from dataclasses import replace

        from .models import CddTier

        case = self.store.load(case_id, principals)
        current = replace(case.current, scorecard=scorecard) if case.current else None
        updated = self._replace(case, scorecard=scorecard, current=current)
        saved = self.store.save(updated, expected_version=case.version)
        decision = Decision.ESCALATED if scorecard.tier is CddTier.EDD else Decision.ALLOWED
        self._record(saved, "sow_case_scorecard", decision, actor)
        return saved

    def attach_source_of_funds(
        self,
        case_id: str,
        principals: tuple[str, ...],
        assessment: SourceOfFundsAssessment,
        actor: str,
    ) -> SowCase:
        """Attach the Source-of-Funds reconciliation (gaps escalate; checker disposes)."""
        from dataclasses import replace

        case = self.store.load(case_id, principals)
        current = replace(case.current, source_of_funds=assessment) if case.current else None
        updated = self._replace(case, source_of_funds=assessment, current=current)
        saved = self.store.save(updated, expected_version=case.version)
        decision = Decision.ESCALATED if assessment.escalates else Decision.ALLOWED
        self._record(saved, "sow_case_source_of_funds", decision, actor)
        return saved

    def attach_monitoring(
        self,
        case_id: str,
        principals: tuple[str, ...],
        assessment: MonitoringAssessment,
        actor: str,
    ) -> SowCase:
        """Attach the ongoing-monitoring outcome (triggers/overdue escalate; checker disposes)."""
        from dataclasses import replace

        case = self.store.load(case_id, principals)
        current = replace(case.current, monitoring=assessment) if case.current else None
        updated = self._replace(case, monitoring=assessment, current=current)
        saved = self.store.save(updated, expected_version=case.version)
        decision = Decision.ESCALATED if assessment.escalates else Decision.ALLOWED
        self._record(saved, "sow_case_monitoring", decision, actor)
        return saved

    def review(
        self,
        case_id: str,
        principals: tuple[str, ...],
        approve: bool,
        checker: str,
    ) -> SowCase:
        """Maker-checker disposition. Approve (four-eyes) seals a snapshot; else re-open."""
        case = self.store.load(case_id, principals)
        if case.status is CaseStatus.READY_FOR_REVIEW:
            case = self.store.save(
                self._replace(case, status=CaseStatus.IN_REVIEW),
                expected_version=case.version,
            )
        if case.status is not CaseStatus.IN_REVIEW:
            self._require(case.status, CaseStatus.IN_REVIEW)

        if not approve:
            updated = self._replace(case, status=CaseStatus.GATHERING)
            saved = self.store.save(updated, expected_version=case.version)
            self._record(saved, "sow_case_review", Decision.ALLOWED, checker)
            return saved

        maker = case.iterations[-1].actor if case.iterations else ""
        if not self.policy.can_approve(maker, checker):
            raise FourEyesError(
                f"checker '{checker}' may not approve their own analysis (maker '{maker}')."
            )
        self._require(case.status, CaseStatus.APPROVED)
        approved = self._replace(case, status=CaseStatus.APPROVED)
        saved = self.store.save(approved, expected_version=case.version)
        if saved.current is not None:
            self.store.seal(
                SowSnapshot(
                    case_id=saved.id,
                    version=saved.version,
                    audit_view=saved.current,
                    approved_by=checker,
                )
            )
        self._record(saved, "sow_case_review", Decision.ALLOWED, checker)
        return saved

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _require(self, src: CaseStatus, dst: CaseStatus) -> None:
        if not self.policy.can(src, dst):
            raise InvalidTransitionError(f"illegal transition {src.value} -> {dst.value}")

    def _drafter(self) -> RfiDraftingService:
        return self.rfi_drafter if self.rfi_drafter is not None else RfiDraftingService()

    @staticmethod
    def _stamp(item: EvidenceItem, round_no: int) -> EvidenceItem:
        from dataclasses import replace

        return replace(item, iteration_no=round_no)

    @staticmethod
    def _replace(case: SowCase, **changes) -> SowCase:
        from dataclasses import replace

        changes.setdefault("version", case.version)  # store bumps on save
        changes["updated_at"] = utcnow()
        return replace(case, **changes)

    def _record(self, case: SowCase, action: str, decision: Decision, actor: str) -> None:
        if self.audit is None:
            return
        summary = f"case={case.id} status={case.status.value} v={case.version}"
        event = AuditEvent(
            action=action,
            actor=actor or "system",
            decision=decision,
            redacted_prompt=f"[{action}] {summary}",
            redacted_response=summary,
            resource="cdd-sow-research",
            metadata={"case_id": case.id, "status": case.status.value},
        )
        # Audit must never break the workflow.
        with contextlib.suppress(Exception):
            self.audit.record(event)
