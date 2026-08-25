"""CddService — the CDD dossier orchestrator (SPEC §5).

Owns the full assessment pipeline and calls only ports. Because the dossier handles
customer PII (rule R1), the complete A1 safety pipeline is mandatory: redact then
guardrail(INPUT) before any model/index/registry call, and guardrail(OUTPUT) before the
dossier is returned. Every consequential output is maker-checker gated (P-06): the
dossier always requires human review.

Pipeline (each step in ``tracer.span``; audited at the end):

    tracer.span("cdd.assess"):
      redact(case inputs)
      -> guardrail.screen(INPUT)             [blocked -> audit BLOCKED + raise]
      -> for each KYC doc: extraction.extract then knowledge_base.ingest (case ACL)
      -> knowledge_base.search                [empty -> RetrievalEmptyError]
      -> adverse_media.scan
      -> ownership.resolve (UBO)
      -> screening (deterministic watchlist match; open alert -> enhanced review)
      -> SourceOfWealthService.build (LLM)
      -> RiskRatingService.rate
      -> compliance.check (C1, regulatory CDD/AML expectations)
      -> assemble CDDCase
      -> guardrail.screen(OUTPUT)             [blocked -> audit BLOCKED + raise]
      -> review policy (always requires_human_review=True; escalation flag)
      -> audit.record(already-redacted)

Defensive throughout: extraction / ingestion / compliance failures degrade rather than
crash, but a blocked input and an ungrounded case are hard errors so a dossier is never
built on screened-out or absent evidence.

Pure domain code: no Google Cloud / ADK / FastAPI imports.
"""

from __future__ import annotations

import contextlib
import logging
from contextlib import nullcontext
from dataclasses import replace
from typing import Any

from . import _grounded as g
from .errors import GuardrailBlockedError, RetrievalEmptyError
from .models import (
    AuditEvent,
    CaseInput,
    CDDCase,
    Citation,
    Decision,
    Direction,
    GuardrailVerdict,
    KycDocument,
    RetrievedPassage,
    RiskRating,
    SourceOfWealthNarrative,
)
from .review_policy import CddReviewPolicy
from .serialization import to_jsonable
from .sow_service import SourceOfWealthService, all_citations

_LOG = logging.getLogger(__name__)


def _own_text(content: bytes) -> bytes:
    """The document's own bytes when they ARE the text, else empty.

    Extraction is built for scanned and laid-out documents, and returns nothing for a plain
    text, CSV or Markdown upload -- which is the honest answer for an extractor and the wrong
    thing to hand a knowledge base, because the text was there all along. A document that is
    already text does not need extracting; it needs indexing.
    """

    if not content:
        return b""
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        return b""
    return content


class CddService:
    """Build a cited CDD dossier for a subject. Constructor takes explicit ports."""

    def __init__(
        self,
        extraction: Any,
        knowledge_base: Any,
        adverse_media: Any,
        registry: Any,
        compliance: Any,
        llm: Any,
        guardrail: Any,
        redaction: Any,
        tracer: Any,
        audit: Any,
        review_policy: CddReviewPolicy | None = None,
        review_router: Any = None,
        document_store: Any = None,
        sanctions: Any = None,
    ) -> None:
        self._extraction = extraction
        self._knowledge_base = knowledge_base
        self._adverse_media = adverse_media
        self._registry = registry
        self._compliance = compliance
        self._llm = llm
        self._guardrail = guardrail
        self._redaction = redaction
        self._tracer = tracer
        self._audit = audit
        self._review = review_policy or CddReviewPolicy()
        # Rule R8: when the dossier requires human review it is routed to Hrz7 (the maker-checker
        # console), not left as a boolean. Optional so unit tests and the CLI can omit it; when
        # unset the escalation still audits ESCALATED, it just is not forwarded to a console.
        self._review_router = review_router
        # Custody of the uploaded documents named by the case. Optional: a case may cite
        # evidence already indexed in the knowledge base (the CLI and unit tests do), in
        # which case there are no bytes to fetch and the pipeline grounds on the index.
        self._document_store = document_store
        # Watchlist snapshot provider (SanctionsListProviderPort). Optional: without it
        # the dossier's ``screening`` stays None (not screened), which is distinct from
        # a result with zero alerts (screened and clear).
        self._sanctions = sanctions

        # Sub-services compose the same ports (explicit-DI per SPEC §5).
        from .adverse_media_service import AdverseMediaService
        from .ownership_service import OwnershipService
        from .risk_service import RiskRatingService
        from .screening import ScreeningPolicy, ScreeningService

        self._sow = SourceOfWealthService(llm=llm, tracer=tracer)
        self._risk = RiskRatingService(llm=llm, tracer=tracer)
        self._adverse = AdverseMediaService(adverse_media=adverse_media, tracer=tracer)
        self._ownership = OwnershipService(registry=registry, tracer=tracer)
        self._screening = ScreeningService()
        self._screening_policy = ScreeningPolicy()

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def assess(
        self,
        case_input: CaseInput,
        actor: str,
        principals: tuple[str, ...] = (),
        tenant: str = "",
    ) -> CDDCase:
        """Assess ``case_input`` and return a cited CDD dossier (SPEC §5).

        ``actor``, ``principals`` and ``tenant`` are the VERIFIED end-user identity
        resolved server-side (see ``api.security``): ``actor`` is the audit subject,
        ``principals`` are the user's entitlement principals, and ``tenant`` (when set)
        is stamped onto the case ACL tags at ingest so retrieval enforces tenant
        isolation, not just case scoping.
        """
        span = self._tracer.span("cdd.assess", action="assess_cdd", actor=actor)
        with span if span is not None else nullcontext():
            return self._assess_inner(case_input, actor, principals, tenant)

    # ------------------------------------------------------------------ #
    # Pipeline
    # ------------------------------------------------------------------ #
    def _assess_inner(
        self,
        case_input: CaseInput,
        actor: str,
        principals: tuple[str, ...] = (),
        tenant: str = "",
    ) -> CDDCase:
        # The request body cannot choose its owning tenant. Stamp the verified server-side
        # tenant onto the dossier itself as well as its retrieval ACL so every downstream
        # hand-off, including Hrz7 review routing, preserves the same tenant boundary.
        subject = replace(case_input.subject, tenant=tenant) if tenant else case_input.subject

        # 1) Redact the case inputs (P-04) before they touch a model, index or audit.
        raw_summary = self._case_summary(case_input)
        redacted_summary = self._redaction.redact(raw_summary).text

        # 2) Guardrail screen (INPUT). Blocked -> audit BLOCKED + raise (no partial dossier).
        in_verdict: GuardrailVerdict = self._guardrail.screen(redacted_summary, Direction.INPUT)
        if not in_verdict.allowed:
            self._write_audit(actor, redacted_summary, "", Decision.BLOCKED)
            raise GuardrailBlockedError(in_verdict.reason or "CDD request blocked by guardrail")

        acl_tags = self._acl_tags(subject.id, tenant)

        # 3) Extract + ingest each KYC document into the governed RAG store (A2, case ACL).
        for document in case_input.documents:
            self._extract_and_ingest(document, acl_tags, (*acl_tags, *principals))

        # 4) Retrieve grounding passages from A2. Empty -> hard error (never ungrounded).
        passages: list[RetrievedPassage] = g.retrieve_passages(
            self._knowledge_base,
            self._retrieval_query(subject),
            # Case ACL tags AND the verified user's entitlement principals: governed
            # retrieval is scoped to what THIS user is allowed to see, server-side.
            acl_principals=(*acl_tags, *principals),
            top_k=self._knowledge_base_top_k(),
        )
        if not passages:
            self._write_audit(actor, redacted_summary, "", Decision.ESCALATED)
            raise RetrievalEmptyError(f"no case evidence retrieved for subject: {subject.id!r}")

        # 5) Adverse media + ownership (UBO) + deterministic watchlist screening.
        # A None screen means none ran; the findings it did not produce are (), but the
        # case keeps the None so the dossier can say "not screened" rather than "clear".
        adverse_media = self._adverse.scan(subject, actor)
        media_findings = adverse_media.findings if adverse_media is not None else ()
        ownership = self._ownership.resolve(subject, actor)
        screening = self._screen(subject)

        # 6) Synthesise the source-of-wealth narrative (LLM, grounded + self-critique).
        sow: SourceOfWealthNarrative = self._sow.build(subject, passages, actor)

        # 7) Risk rating (LLM + deterministic hard-signal raise).
        rating: RiskRating = self._risk.rate(
            subject, sow, media_findings, ownership, passages, actor
        )

        # 8) Check against regulatory CDD/AML expectations via C1 (best-effort).
        self._compliance_check(subject, rating, actor)

        # 9) Assemble the dossier.
        case = CDDCase(
            id=f"cdd-{subject.id}",
            subject=subject,
            sow=sow,
            rating=rating,
            adverse_media=adverse_media,
            ownership=ownership,
            screening=screening,
            requires_human_review=self._review.requires_review(),
        )

        # 10) Guardrail screen (OUTPUT) on the assembled narrative + rationale.
        out_text = f"{sow.narrative}\n{rating.rationale}"
        out_verdict: GuardrailVerdict = self._guardrail.screen(out_text, Direction.OUTPUT)
        if not out_verdict.allowed:
            self._write_audit(actor, redacted_summary, "", Decision.BLOCKED, direction="output")
            raise GuardrailBlockedError(out_verdict.reason or "CDD dossier blocked by guardrail")

        # 11) Review policy: a dossier is consequential, so it is always routed to a
        #     human checker (audit decision ESCALATED); hard signals set an extra flag.
        # An open watchlist alert always escalates to enhanced review; it never
        # auto-blocks (soft disposition under maker-checker, see ScreeningPolicy).
        escalated = self._review.escalates(
            rating.band, media_findings
        ) or self._screening_policy.requires_enhanced_review(screening)

        # 12) Audit (already-redacted prompt + a redacted response summary).
        self._audit_case(actor, redacted_summary, case, Decision.ESCALATED, escalated)

        # 13) Route the escalation to Hrz7 (rule R8). A dossier always requires human review, so
        #     it is handed to the maker-checker console rather than terminating in a boolean; the
        #     adapter redacts before the wire. Best-effort: a console outage must not fail an
        #     already-assembled, already-audited dossier (the audit ESCALATED record is the
        #     durable escalation of record, and the outbox path retries).
        if self._review_router is not None and case.requires_human_review:
            # Routing is a hand-off, never fatal to an already-assembled, already-audited dossier.
            with contextlib.suppress(Exception):
                self._review_router.route(case, maker=actor)
        return case

    # ------------------------------------------------------------------ #
    # Steps
    # ------------------------------------------------------------------ #
    def _extract_and_ingest(
        self,
        document: KycDocument,
        acl_tags: tuple[str, ...],
        read_principals: tuple[str, ...] = (),
    ) -> None:
        """Fetch, extract and ingest one KYC document into A2; best-effort per document.

        The bytes come from the document store under the READER's principals, not the
        case tags alone: custody is checked with the same fail-closed ACL as retrieval,
        so naming another tenant's document id in a request body yields nothing.

        The stored record also supplies the document's location, replacing whatever the
        request body claimed. A citation's link is a security-relevant field: taking it
        from the client would let a request decide where a reviewer is sent when they
        click "source" on a dossier.
        """
        content, mime_type, uri = self._fetch_document(document, read_principals)
        if uri:
            document = replace(document, uri=uri)
        try:
            extract = self._extraction.extract(document, content, mime_type)
        except Exception:  # noqa: BLE001 - a single bad document must not fail the case
            extract = None
        if extract is not None and extract.pages and self._document_store is not None:
            # Record the page count so a reviewer sees document length in the case file.
            with contextlib.suppress(Exception):
                self._document_store.set_pages(document.id, extract.pages)
        try:
            text = extract.text if extract is not None else ""
            page_texts = extract.page_texts if extract is not None else ()
            self._knowledge_base.ingest(
                document,
                text.encode("utf-8") or _own_text(content),
                acl_tags,
                page_texts=page_texts,
            )
        except Exception:  # noqa: BLE001 - ingestion is best-effort; retrieval is the gate
            return

    def _fetch_document(
        self, document: KycDocument, read_principals: tuple[str, ...]
    ) -> tuple[bytes, str, str]:
        """The stored bytes, media type and canonical location for ``document``.

        All three come from the custody record, never the request. Empty bytes are the
        honest answer for a case whose evidence is already indexed (the CLI and the
        offline tests): extraction then returns an empty extract rather than inventing
        document content.
        """
        if self._document_store is None:
            return b"", self._mime_for(document), ""
        try:
            content: bytes = self._document_store.get(document.id, read_principals)
            record = self._document_store.metadata(document.id, read_principals)
        except Exception:  # noqa: BLE001 - an unreadable document degrades, never crashes
            return b"", self._mime_for(document), ""
        return (
            content,
            getattr(record, "mime_type", "") or self._mime_for(document),
            getattr(record, "uri", "") or "",
        )

    def _screen(self, subject: Any) -> Any:
        """Deterministic watchlist screening against the synced snapshot (best-effort).

        Returns None when no provider is wired OR the snapshot cannot be read: "not
        screened" is the honest answer then, and it is visibly distinct in the dossier
        from a real result with zero alerts. A screening outage therefore degrades the
        dossier rather than failing it; the maker-checker gate still applies.

        The degradation is deliberate. Its SILENCE was not. This returned None on any
        exception and emitted nothing -- no log, no span attribute, nothing an operator
        could have queried -- so a deployment whose watchlist snapshot did not exist ran
        for as long as it existed producing dossiers that had screened nobody, and the
        only reason anyone found out was that a paired run compared it against a laptop
        that had. "Not screened" was on the wire the whole time and read as an absence
        rather than as an outage.

        So the outcome is recorded on the way out, both branches. An unwired provider is a
        configuration fact and is logged once at INFO; a provider that was wired and failed
        is an OUTAGE and is logged at ERROR with the cause, because those are two different
        events that had been producing one indistinguishable None.
        """
        if self._sanctions is None:
            _LOG.info(
                "watchlist screening skipped for subject %s: no sanctions provider is bound "
                "under this profile; the dossier will report NOT SCREENED",
                getattr(subject, "id", "?"),
            )
            return None
        span = self._tracer.span("cdd.screen", action="screen_subject", actor=subject.id)
        with span if span is not None else nullcontext():
            try:
                return self._screening.screen_subject(subject, self._sanctions)
            except Exception as exc:  # noqa: BLE001 - an unreadable snapshot degrades, never crashes
                _LOG.error(
                    "watchlist screening FAILED for subject %s and the dossier will report "
                    "NOT SCREENED: %s: %s. The snapshot is expected at the configured "
                    "sanctions bucket/object; a dossier produced now has screened nobody.",
                    getattr(subject, "id", "?"),
                    type(exc).__name__,
                    exc,
                )
                return None

    def _compliance_check(self, subject: Any, rating: RiskRating, actor: str) -> None:
        """Ask C1 whether the rating meets regulatory CDD/AML expectations (best-effort)."""
        question = (
            f"For a {subject.type.value} customer in {subject.jurisdiction or 'an unknown'} "
            f"jurisdiction rated {rating.band.value} risk, what CDD/AML expectations apply?"
        )
        try:
            self._compliance.check(question, actor)
        except Exception:  # noqa: BLE001 - the C1 check is advisory, never fatal here
            return

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _case_summary(case_input: CaseInput) -> str:
        subject = case_input.subject
        docs = ", ".join(f"{d.id}:{d.doc_type.value}" for d in case_input.documents) or "none"
        dob = subject.dob_or_incorp or "unknown"
        return (
            f"CDD case for subject {subject.name} (id={subject.id}, type={subject.type.value}, "
            f"jurisdiction={subject.jurisdiction or 'unknown'}, dob_or_incorp={dob}); "
            f"documents=[{docs}]"
        )

    @staticmethod
    def _acl_tags(subject_id: str, tenant: str = "") -> tuple[str, ...]:
        """Case ACL tags stamped onto ingested evidence.

        With a tenant, evidence carries BOTH ``case:<id>`` and ``tenant:<tenant>``; the
        knowledge-base subset match then requires a reader to hold both, so a case id
        alone never crosses a tenant boundary (object-level authorization). Shared with
        the document store via ``entitlements.case_tags`` so custody and retrieval are
        scoped identically.
        """
        from .entitlements import case_tags

        return case_tags(subject_id, tenant)

    @staticmethod
    def _retrieval_query(subject: Any) -> str:
        return (
            f"source of wealth, ownership and risk evidence for {subject.name} "
            f"({subject.type.value})"
        )

    def _knowledge_base_top_k(self) -> int:
        settings = getattr(self._knowledge_base, "settings", None)
        kb = getattr(settings, "knowledge_base", None)
        return getattr(kb, "top_k", 10)

    @staticmethod
    def _mime_for(document: KycDocument) -> str:
        return "application/pdf"

    # ------------------------------------------------------------------ #
    # Audit
    # ------------------------------------------------------------------ #
    def _audit_case(
        self,
        actor: str,
        redacted_prompt: str,
        case: CDDCase,
        decision: Decision,
        escalated: bool,
    ) -> None:
        citations = self._case_citations(case)
        screening = (
            "not_screened"
            if case.screening is None
            else f"{len(case.screening.open_alerts)} open alerts"
        )
        media = (
            "not screened"
            if case.adverse_media is None
            else f"{len(case.adverse_media.findings)} findings"
        )
        summary = (
            f"risk={case.rating.band.value}; sow_sources={len(case.sow.sources)}; "
            f"adverse_media={media}; "
            f"owners={len(case.ownership.owners) if case.ownership else 0}; "
            f"screening={screening}"
        )
        self._write_audit(
            actor,
            redacted_prompt,
            summary,
            decision,
            citations=citations,
            metadata={
                "risk_band": case.rating.band.value,
                "requires_human_review": str(case.requires_human_review).lower(),
                "escalated": str(escalated).lower(),
                "n_citations": str(len(citations)),
            },
        )

    @staticmethod
    def _case_citations(case: CDDCase) -> tuple[Citation, ...]:
        out: list[Citation] = []
        out.extend(all_citations(case.sow))
        out.extend(case.rating.citations)
        for f in case.adverse_media.findings if case.adverse_media is not None else ():
            if f.citation is not None:
                out.append(f.citation)
        if case.ownership is not None:
            out.extend(case.ownership.citations)
            for owner in case.ownership.owners:
                out.extend(owner.citations)
        seen: set[tuple[str, int | None]] = set()
        deduped: list[Citation] = []
        for c in out:
            key = (c.source_id, c.page)
            if key not in seen:
                seen.add(key)
                deduped.append(c)
        return tuple(deduped)

    def _write_audit(
        self,
        actor: str,
        redacted_prompt: str,
        redacted_response: str,
        decision: Decision,
        citations: tuple[Citation, ...] = (),
        metadata: dict[str, str] | None = None,
        direction: str = "input",
    ) -> None:
        event = AuditEvent(
            action="assess_cdd",
            actor=actor,
            decision=decision,
            redacted_prompt=redacted_prompt,
            redacted_response=redacted_response,
            citations=citations,
            metadata={**(metadata or {}), "direction": direction},
        )
        try:
            self._audit.record(event)
        except Exception:  # noqa: BLE001 - audit failure must not crash the request
            to_jsonable(event)
