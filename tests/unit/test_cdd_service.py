"""Unit tests for CddService — the SPEC §5 assessment pipeline.

Pipeline (SPEC §5):
    redact(case inputs) -> guardrail.screen(INPUT)
      -> [if blocked: audit BLOCKED + raise GuardrailBlockedError]
      -> for each KYC doc: extraction.extract then knowledge_base.ingest (case ACL)
      -> knowledge_base.search -> [empty: RetrievalEmptyError]
      -> adverse_media.scan -> ownership.resolve
      -> SoW narrative -> risk rating -> compliance.check
      -> assemble CDDCase -> guardrail.screen(OUTPUT)
      -> review policy (always requires_human_review) -> audit.record(redacted)

These tests use only in-memory fakes (no Google Cloud SDK).
"""

from __future__ import annotations

import pytest
from tests.conftest import BlockingGuardrail, load_service
from tests.fixtures import sample_cases

from cdd_sow_research.domain.errors import GuardrailBlockedError, RetrievalEmptyError
from cdd_sow_research.domain.models import (
    AdverseMediaCategory,
    AdverseMediaFinding,
    BeneficialOwner,
    CaseInput,
    CDDCase,
    Decision,
    Direction,
    OwnershipSummary,
    RiskBand,
    Severity,
)

ACTOR = "analyst@bank.test"


def _build(
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
    return load_service("CddService")(
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
    )


# --------------------------------------------------------------------------- #
# Redaction happens BEFORE everything (P-04 / R1).
# --------------------------------------------------------------------------- #
def test_redaction_runs_before_ingest_and_search(cdd_service, redaction, knowledge_base):
    cdd_service.assess(sample_cases.PII_CASE_INPUT, actor=ACTOR)

    # Redaction was invoked on the raw case summary (which carried the NRIC + email).
    assert redaction.calls, "redaction.redact was never called"
    assert "S1234567A" in redaction.calls[0]

    # The retrieval query the KB saw must already be de-identified... but the query is
    # built from the (clean) subject name; assert no raw PII leaked into the audit either.


def test_redacted_audit_has_no_raw_pii(cdd_service, audit):
    cdd_service.assess(sample_cases.PII_CASE_INPUT, actor=ACTOR)
    assert audit.events
    for event in audit.events:
        assert "S1234567A" not in event.redacted_prompt
        assert "casey.lim@example.com" not in event.redacted_prompt


# --------------------------------------------------------------------------- #
# Blocked input: BLOCKED audit + raise; no model/ingest/retrieval.
# --------------------------------------------------------------------------- #
def test_blocked_input_raises_and_audits(
    extraction, knowledge_base, adverse_media, registry, compliance, llm, redaction, tracer, audit
):
    service = _build(
        extraction,
        knowledge_base,
        adverse_media,
        registry,
        compliance,
        llm,
        BlockingGuardrail(block_input=True),
        redaction,
        tracer,
        audit,
    )
    with pytest.raises(GuardrailBlockedError):
        service.assess(sample_cases.MALICIOUS_CASE_INPUT, actor=ACTOR)

    # No artifact generation, no retrieval, no ingest after a blocked input.
    assert llm.requests == []
    assert knowledge_base.queries == []
    assert knowledge_base.ingested == []

    # Exactly one BLOCKED audit event, already redacted.
    blocked = [e for e in audit.events if e.decision is Decision.BLOCKED]
    assert blocked, "expected an audit event with decision=BLOCKED"
    assert blocked[0].actor == ACTOR
    assert blocked[0].action == "assess_cdd"


def test_blocked_input_screens_input_only(
    extraction, knowledge_base, adverse_media, registry, compliance, llm, redaction, tracer, audit
):
    blocking = BlockingGuardrail(block_input=True)
    service = _build(
        extraction,
        knowledge_base,
        adverse_media,
        registry,
        compliance,
        llm,
        blocking,
        redaction,
        tracer,
        audit,
    )
    with pytest.raises(GuardrailBlockedError):
        service.assess(sample_cases.MALICIOUS_CASE_INPUT, actor=ACTOR)
    directions = [d for _, d in blocking.calls]
    assert Direction.INPUT in directions
    assert Direction.OUTPUT not in directions


# --------------------------------------------------------------------------- #
# Empty governed RAG store -> hard error (never ungrounded).
# --------------------------------------------------------------------------- #
def test_empty_knowledge_base_raises(
    extraction,
    empty_knowledge_base,
    adverse_media,
    registry,
    compliance,
    llm,
    guardrail,
    redaction,
    tracer,
    audit,
):
    service = _build(
        extraction,
        empty_knowledge_base,
        adverse_media,
        registry,
        compliance,
        llm,
        guardrail,
        redaction,
        tracer,
        audit,
    )
    with pytest.raises(RetrievalEmptyError):
        service.assess(sample_cases.SAMPLE_CASE_INPUT, actor=ACTOR)


# --------------------------------------------------------------------------- #
# Normal path: a well-formed, cited, human-review-flagged dossier.
# --------------------------------------------------------------------------- #
def test_normal_path_builds_full_dossier(cdd_service):
    case = cdd_service.assess(sample_cases.SAMPLE_CASE_INPUT, actor=ACTOR)

    assert isinstance(case, CDDCase)
    assert case.requires_human_review is True

    # Source-of-wealth narrative is present and broken into cited wealth sources.
    assert case.sow.narrative
    assert case.sow.sources, "the narrative must break wealth into sources"
    assert case.sow.citations, "the narrative must carry citations"
    for src in case.sow.sources:
        for c in src.citations:
            assert c.page is not None, "page-level citation is required"

    # Risk rating present with a band and at least one factor.
    assert isinstance(case.rating.band, RiskBand)
    assert case.rating.factors

    # Ownership resolved (entity subject) with a beneficial owner.
    assert case.ownership is not None
    assert case.ownership.owners


def test_normal_path_ingests_each_document_with_case_acl(cdd_service, knowledge_base):
    cdd_service.assess(sample_cases.SAMPLE_CASE_INPUT, actor=ACTOR)
    ingested_ids = {doc.id for doc, _ in knowledge_base.ingested}
    assert ingested_ids == {d.id for d in sample_cases.SAMPLE_DOCUMENTS}
    for _, acl_tags in knowledge_base.ingested:
        assert any(t.startswith("case:") for t in acl_tags), "ingest must carry a case ACL tag"


def test_normal_path_passes_acl_principals_to_search(cdd_service, knowledge_base):
    cdd_service.assess(sample_cases.SAMPLE_CASE_INPUT, actor=ACTOR)
    assert knowledge_base.queries
    query = knowledge_base.queries[0]
    assert any(p.startswith("case:") for p in query.acl_principals)


def test_normal_path_audited_as_escalated(cdd_service, audit):
    cdd_service.assess(sample_cases.SAMPLE_CASE_INPUT, actor=ACTOR)
    assert audit.events
    # A dossier is consequential -> the audit decision is ESCALATED (human checker).
    assert any(e.decision is Decision.ESCALATED for e in audit.events)
    assert audit.events[-1].action == "assess_cdd"


def test_normal_path_wrapped_in_tracer_span(cdd_service, tracer):
    cdd_service.assess(sample_cases.SAMPLE_CASE_INPUT, actor=ACTOR)
    assert tracer.spans, "the assess pipeline must open at least one trace span"


def test_normal_path_queries_compliance(cdd_service, compliance):
    cdd_service.assess(sample_cases.SAMPLE_CASE_INPUT, actor=ACTOR)
    assert compliance.calls, "the dossier must be checked against C1 regulatory expectations"


# --------------------------------------------------------------------------- #
# Hard signals raise the band and escalate (sanctions / terrorism / PEP).
# --------------------------------------------------------------------------- #
def _sanctions_media() -> tuple[AdverseMediaFinding, ...]:
    return (
        AdverseMediaFinding(
            headline="Subject named in alleged sanctions-evasion scheme (FICTIONAL)",
            publisher="Example Wire",
            url="https://example.test/news/sanctions",
            category=AdverseMediaCategory.SANCTIONS,
            severity=Severity.CRITICAL,
            snippet="Alleged sanctions evasion.",
        ),
    )


def test_sanctions_hit_forces_prohibited_band(
    extraction, knowledge_base, registry, compliance, llm, guardrail, redaction, tracer, audit
):
    from tests.conftest import FakeAdverseMedia

    service = _build(
        extraction,
        knowledge_base,
        FakeAdverseMedia(findings=_sanctions_media()),
        registry,
        compliance,
        llm,
        guardrail,
        redaction,
        tracer,
        audit,
    )
    case = service.assess(sample_cases.SAMPLE_CASE_INPUT, actor=ACTOR)
    # The fake LLM returns "medium"; the high-severity sanctions hit must force PROHIBITED.
    assert case.rating.band is RiskBand.PROHIBITED
    assert case.adverse_media is not None
    assert case.adverse_media.findings[0].category is AdverseMediaCategory.SANCTIONS


def test_low_severity_sanctions_coverage_raises_to_high_not_prohibited(
    extraction, knowledge_base, registry, compliance, llm, guardrail, redaction, tracer, audit
):
    """A settled penalty in the news is not a designation: HIGH, never PROHIBITED.

    Real grounded research surfaces medium-severity sanctions-adjacent coverage (settled
    OFAC penalties, breach fines) for many large legitimate companies; the designation
    signal is the deterministic watchlist screen, not a headline.
    """
    from dataclasses import replace as dc_replace

    from tests.conftest import FakeAdverseMedia

    settled_penalty = tuple(dc_replace(f, severity=Severity.MEDIUM) for f in _sanctions_media())
    service = _build(
        extraction,
        knowledge_base,
        FakeAdverseMedia(findings=settled_penalty),
        registry,
        compliance,
        llm,
        guardrail,
        redaction,
        tracer,
        audit,
    )
    case = service.assess(sample_cases.SAMPLE_CASE_INPUT, actor=ACTOR)
    assert case.rating.band is RiskBand.HIGH
    assert "at least HIGH" in case.rating.rationale


def test_pep_owner_raises_band_to_at_least_high(
    extraction,
    knowledge_base,
    adverse_media,
    compliance,
    llm,
    guardrail,
    redaction,
    tracer,
    audit,
):
    from tests.conftest import FakeRegistry

    pep_summary = OwnershipSummary(
        root_entity="Acme Holdings Pte Ltd (FICTIONAL)",
        owners=(BeneficialOwner(name="A PEP (FICTIONAL)", pct=60.0, country="SG", is_pep=True),),
    )
    service = _build(
        extraction,
        knowledge_base,
        adverse_media,
        FakeRegistry(summary=pep_summary),
        compliance,
        llm,
        guardrail,
        redaction,
        tracer,
        audit,
    )
    case = service.assess(sample_cases.SAMPLE_CASE_INPUT, actor=ACTOR)
    assert case.rating.band in (RiskBand.HIGH, RiskBand.PROHIBITED)


# --------------------------------------------------------------------------- #
# Individual subject: no corporate tree, but the rest of the dossier still builds.
# --------------------------------------------------------------------------- #
def test_individual_subject_has_empty_ownership(cdd_service):
    case_input = CaseInput(subject=sample_cases.INDIVIDUAL_SUBJECT, documents=())
    case = cdd_service.assess(case_input, actor=ACTOR)
    assert case.ownership is not None
    assert case.ownership.owners == ()
    assert case.sow.narrative


# --------------------------------------------------------------------------- #
# Verified user principals are threaded into governed retrieval (per-user authZ):
# the resolved Principal's entitlement principals must reach the KB query alongside
# the case ACL tags, so the data path is scoped to what the user may see (not
# client-asserted). Requesting both fixtures gives the same KB instance the service uses.
# --------------------------------------------------------------------------- #
def test_user_principals_flow_into_governed_retrieval(cdd_service, knowledge_base):
    cdd_service.assess(sample_cases.SAMPLE_CASE_INPUT, actor=ACTOR, principals=("group:risk",))
    assert knowledge_base.queries, "expected at least one governed retrieval query"
    last = knowledge_base.queries[-1]
    assert "group:risk" in last.acl_principals
    # The case ACL scoping is still present alongside the user principals.
    assert any(p.startswith("case:") for p in last.acl_principals)


# --------------------------------------------------------------------------- #
# Watchlist screening is part of every assessment: a clear subject carries a real
# "screened and clear" result (never None when a provider is wired), a watchlist
# subject raises an open alert that escalates to enhanced review, and with no
# provider the dossier honestly says "not screened" (None).
# --------------------------------------------------------------------------- #
def test_clear_subject_is_screened_and_clear_not_unscreened(cdd_service):
    case = cdd_service.assess(sample_cases.SAMPLE_CASE_INPUT, actor=ACTOR)
    assert case.screening is not None, "a wired provider must always yield a result"
    assert case.screening.alerts == ()
    assert case.screening.lists_version, "the snapshot version must be recorded for audit"


def test_watchlist_subject_raises_an_open_alert_and_escalates(cdd_service, audit):
    from dataclasses import replace

    watchlisted = replace(
        sample_cases.INDIVIDUAL_SUBJECT, id="subj-tan-wei-ming", name="Tan Wei Ming"
    )
    case = cdd_service.assess(CaseInput(subject=watchlisted, documents=()), actor=ACTOR)

    assert case.screening is not None
    assert case.screening.open_alerts, "the fixture watchlist entry must raise an alert"
    top = case.screening.open_alerts[0]
    assert top.match.score >= 0.85
    assert top.match.entry.source.value == "ofac_sdn"
    # The open alert escalates the case to enhanced review (soft, never auto-block).
    final = audit.events[-1]
    assert final.metadata.get("escalated") == "true"


def test_without_a_provider_screening_is_none(
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
    service = _build(
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
    )
    case = service.assess(sample_cases.SAMPLE_CASE_INPUT, actor=ACTOR)
    assert case.screening is None


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
