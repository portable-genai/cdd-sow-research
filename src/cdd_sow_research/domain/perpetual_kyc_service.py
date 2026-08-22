"""PerpetualKycService — the perpetual-KYC orchestrator (SPEC §5).

Runs one perpetual-KYC cycle for a subject inside the existing CDD lifecycle:

1. observe the three monitored edges through the ports the dossier pipeline already uses
   (``SanctionsListProviderPort`` screening, ``AdverseMediaPort``, ``CorporateRegistryPort``);
2. load the stored baseline through the ``MonitoringStorePort`` (fail-closed on ACL);
3. compute the whole outcome in PURE code with
   :class:`~cdd_sow_research.domain.perpetual_kyc.PerpetualKycEngine` (signals, the re-score
   and its per-signal uplift lines, the deterministic queue priority and SLA date);
4. optionally ask the LLM to NARRATE that finished outcome, after redaction, with the
   numbers already fixed: the narrative is prose about a decision the model did not make;
5. route the assessment to Hrz7 for maker-checker disposition (rule R8) and persist it,
   then advance the baseline; and
6. write the already-redacted WORM audit record.

Every consequential value is arithmetic the engine performed and an auditor can recompute.
The service never blocks, freezes or downgrades a relationship: it queues an explainable
review. A narration, screening, media, registry or routing failure degrades gracefully
(the deterministic assessment still stands) except for a store failure, which is fatal:
losing the baseline would make unchanged facts look new on the next run.

Pure domain code: talks only to ports and models, no Google Cloud / ADK imports.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from . import _grounded as g
from .adverse_media_service import AdverseMediaService
from .entitlements import case_tags
from .models import (
    AdverseMediaFinding,
    AuditEvent,
    Decision,
    MonitoringAssessment,
    OwnershipSummary,
    PerpetualKycAssessment,
    ScreeningResult,
    Subject,
    SubjectType,
    ThinkingLevel,
)
from .ownership_service import OwnershipService
from .periodic_review_service import PeriodicReviewService
from .perpetual_kyc import PerpetualKycEngine
from .policy import RiskPolicy
from .prompts import NARRATIVE_SCHEMA, PERPETUAL_KYC_NARRATIVE_PROMPT
from .screening import ScreeningService

_LOG = logging.getLogger(__name__)

#: Hard cap on the narrative the model may return. The narrative is decoration on a
#: decision the code already made, so an over-long answer is truncated, never trusted.
_MAX_NARRATIVE_CHARS = 1200


class PerpetualKycService:
    """Orchestrate one perpetual-KYC cycle. Signature fixed by SPEC §5."""

    def __init__(
        self,
        *,
        sanctions: Any,
        adverse_media: Any,
        registry: Any,
        store: Any,
        review_router: Any,
        audit: Any,
        tracer: Any,
        redaction: Any | None = None,
        llm: Any | None = None,
        engine: PerpetualKycEngine | None = None,
        periodic_review: PeriodicReviewService | None = None,
        screening: ScreeningService | None = None,
    ) -> None:
        self._sanctions = sanctions
        self._adverse_media = adverse_media
        self._registry = registry
        self._store = store
        self._review_router = review_router
        self._audit = audit
        self._tracer = tracer
        self._redaction = redaction
        self._llm = llm
        self._engine = engine or PerpetualKycEngine()
        self._periodic_review = periodic_review or PeriodicReviewService()
        self._screening = screening or ScreeningService()

    @classmethod
    def from_policy(
        cls,
        policy: RiskPolicy,
        *,
        sanctions: Any,
        adverse_media: Any,
        registry: Any,
        store: Any,
        review_router: Any,
        audit: Any,
        tracer: Any,
        redaction: Any | None = None,
        llm: Any | None = None,
    ) -> PerpetualKycService:
        """Build the service with every number taken from bank-owned policy (B4)."""
        return cls(
            sanctions=sanctions,
            adverse_media=adverse_media,
            registry=registry,
            store=store,
            review_router=review_router,
            audit=audit,
            tracer=tracer,
            redaction=redaction,
            llm=llm,
            engine=PerpetualKycEngine.from_policy(policy.perpetual_kyc),
            periodic_review=PeriodicReviewService.from_policy(policy.monitoring),
        )

    # ------------------------------------------------------------------ #
    def run(
        self,
        subject: Subject,
        *,
        actor: str,
        principals: tuple[str, ...],
        as_of: date,
        last_reviewed: str = "",
        narrate: bool = True,
    ) -> PerpetualKycAssessment:
        """Run one cycle and return the persisted, routed assessment.

        ``principals`` are the server-derived ACL principals for this subject (from the
        VERIFIED principal, never a client-supplied actor). The record's own ACL is
        stamped from the subject, so a later read by another tenant is refused.
        """
        with self._tracer.span("perpetual_kyc.run", action="perpetual_kyc", actor=actor):
            screening = self._screen(subject)
            media = self._scan_media(subject, actor)
            ownership = self._resolve_ownership(subject, actor)
            baseline = self._store.load_baseline(subject.id, principals)

            monitoring = self._monitoring(
                subject, screening, media, last_reviewed=last_reviewed, as_of=as_of
            )
            assessment = self._engine.assess(
                subject=subject,
                as_of=as_of,
                baseline=baseline,
                screening=screening,
                adverse_media=media,
                ownership=ownership,
                monitoring=monitoring,
                acl=case_tags(subject.id, subject.tenant),
            )
            if narrate:
                assessment = self._narrate(assessment)

            routed = self._route(assessment, maker=actor)
            assessment = _with_routed_flag(assessment, routed)

            self._store.record(assessment)
            self._store.save_baseline(self._engine.next_baseline(assessment))
            self._write_audit(assessment, actor=actor, routed=routed)
            return assessment

    def queue(self, principals: tuple[str, ...]) -> tuple[PerpetualKycAssessment, ...]:
        """The caller's explainable review queue, most urgent first (tenant-scoped)."""
        return tuple(self._store.queue(principals))

    # ------------------------------------------------------------------ #
    # Observation (each edge is best-effort: a dead feed must not lose the cycle)
    # ------------------------------------------------------------------ #
    def _screen(self, subject: Subject) -> ScreeningResult | None:
        try:
            return self._screening.screen_subject(subject, self._sanctions)
        except Exception:  # noqa: BLE001 - a screening outage degrades, never fails the run
            _LOG.warning("perpetual-KYC screening unavailable for %s", subject.id)
            return None

    def _scan_media(self, subject: Subject, actor: str) -> tuple[AdverseMediaFinding, ...]:
        """The findings a screen returned, or () when no screen ran.

        A perpetual-KYC cycle diffs the CURRENT picture against the last one, so a screen
        that did not run must not be diffed as "the findings went away": callers treat ()
        as "nothing to compare", never as evidence that previous findings cleared.
        """
        service = AdverseMediaService(adverse_media=self._adverse_media, tracer=self._tracer)
        screening = service.scan(subject, actor)
        return screening.findings if screening is not None else ()

    def _resolve_ownership(self, subject: Subject, actor: str) -> OwnershipSummary | None:
        if subject.type is not SubjectType.ENTITY:
            return None
        service = OwnershipService(registry=self._registry, tracer=self._tracer)
        summary = service.resolve(subject, actor)
        return summary if summary.owners else None

    def _monitoring(
        self,
        subject: Subject,
        screening: ScreeningResult | None,
        media: tuple[AdverseMediaFinding, ...],
        *,
        last_reviewed: str,
        as_of: date,
    ) -> MonitoringAssessment:
        """The risk-based schedule half, reusing the existing periodic-review engine."""
        triggers = self._periodic_review.triggers_from_signals(screening=screening)
        if media:
            from .models import ReviewTrigger, ReviewTriggerKind, Severity

            triggers.append(
                ReviewTrigger(
                    kind=ReviewTriggerKind.ADVERSE_MEDIA,
                    severity=max(
                        (f.severity for f in media),
                        key=lambda s: ("low", "medium", "high", "critical").index(s.value),
                        default=Severity.MEDIUM,
                    ),
                    summary=f"{len(media)} adverse-media finding(s) in the current scan.",
                    detail="Negative news is attached to the relationship and needs review.",
                )
            )
        return self._periodic_review.assess(
            subject.id, None, last_reviewed, as_of, triggers=triggers
        )

    # ------------------------------------------------------------------ #
    # Narration: prose only, after redaction, over numbers the code already fixed
    # ------------------------------------------------------------------ #
    def _narrate(self, assessment: PerpetualKycAssessment) -> PerpetualKycAssessment:
        if self._llm is None:
            return assessment
        facts = self._facts(assessment)
        if self._redaction is not None:
            try:
                facts = self._redaction.redact(facts).text
            except Exception:  # noqa: BLE001 - never send un-redacted text to a model
                _LOG.warning("perpetual-KYC redaction failed; narration skipped")
                return assessment
        try:
            response = self._llm.generate(
                g.build_llm_request(
                    PERPETUAL_KYC_NARRATIVE_PROMPT,
                    facts,
                    None,
                    NARRATIVE_SCHEMA,
                    thinking=ThinkingLevel.LOW,
                    max_output_tokens=512,
                )
            )
            # Schema-validated: the model answers a declared JSON shape, and anything
            # that is not a non-empty ``narrative`` string is discarded rather than
            # rendered. The numbers are never read back from the model.
            payload = g.parse_structured(response)
        except Exception:  # noqa: BLE001 - narration is decoration, never load-bearing
            _LOG.warning("perpetual-KYC narration unavailable for %s", assessment.subject_id)
            return assessment
        g.maybe_record_usage(self._tracer, response)
        text = payload.get("narrative")
        if not isinstance(text, str) or not text.strip():
            return assessment
        from dataclasses import replace

        return replace(assessment, narrative=text.strip()[:_MAX_NARRATIVE_CHARS])

    @staticmethod
    def _facts(assessment: PerpetualKycAssessment) -> str:
        """The already-computed facts the narrator may restate, and nothing else."""
        lines = [
            f"subject_id: {assessment.subject_id}",
            f"as_of: {assessment.as_of}",
            f"baseline_score: {assessment.baseline_score:.4f}",
            f"score: {assessment.score:.4f}",
            f"score_delta: {assessment.score_delta:.4f}",
            f"band: {assessment.baseline_band.value} -> {assessment.band.value}",
            f"tier: {assessment.tier.value}",
        ]
        item = assessment.queue_item
        if item is not None:
            lines.append(f"priority: {item.priority.value}")
            lines.append(f"sla_due: {item.sla_due}")
            lines.extend(f"reason: {r}" for r in item.reasons)
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    def _route(self, assessment: PerpetualKycAssessment, *, maker: str) -> bool:
        try:
            self._review_router.route_monitoring(assessment, maker=maker)
        except Exception:  # noqa: BLE001 - a console outage must not lose the assessment
            _LOG.error(
                "perpetual-KYC review routing failed for %s; the assessment stays queued "
                "locally and requires human review",
                assessment.subject_id,
            )
            return False
        return True

    def _write_audit(self, assessment: PerpetualKycAssessment, *, actor: str, routed: bool) -> None:
        item = assessment.queue_item
        try:
            self._audit.record(
                AuditEvent(
                    action="perpetual_kyc.rescore",
                    actor=actor,
                    decision=Decision.ESCALATED,
                    redacted_prompt=f"perpetual-KYC cycle for {assessment.subject_id}",
                    redacted_response=assessment.rationale,
                    citations=assessment.citations,
                    metadata={
                        "subject_id": assessment.subject_id,
                        "as_of": assessment.as_of,
                        "band": assessment.band.value,
                        "priority": item.priority.value if item is not None else "standard",
                        "routed_to_hrz7": str(routed).lower(),
                        "requires_human_review": "true",
                    },
                )
            )
        except Exception:  # noqa: BLE001 - audit failure must not lose the assessment
            _LOG.exception("perpetual-KYC audit write failed for %s", assessment.subject_id)


def _with_routed_flag(assessment: PerpetualKycAssessment, routed: bool) -> PerpetualKycAssessment:
    from dataclasses import replace

    if assessment.queue_item is None:
        return assessment
    return replace(
        assessment,
        queue_item=replace(assessment.queue_item, routed_to_hrz7=routed),
    )
