"""R8 routing: an escalated CDD dossier is routed to Hrz7 via the shared review-kit.

Every CDD dossier requires human review (P-06), so rule R8 says it MUST be handed to the Hrz7
maker-checker console rather than left as a boolean. These tests prove the producer half of that
loop end-to-end against the offline local router (an in-memory outbox), and prove the redact-
before-wire boundary so no raw customer identifier reaches the console.
"""

from __future__ import annotations

import pytest
from tests.conftest import load_service
from tests.fixtures import sample_cases

from cdd_sow_research.adapters._review_payload import assessment_to_review, case_to_review
from cdd_sow_research.adapters.local.review_router import LocalReviewRouter
from cdd_sow_research.config import Settings
from cdd_sow_research.domain.models import (
    CDDCase,
    Citation,
    RiskBand,
    RiskRating,
    SourceOfWealthNarrative,
    SourceType,
    Subject,
    SubjectType,
)

ACTOR = "analyst@bank.test"


def _service_with_router(
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
    router,
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
        review_router=router,
    )


def test_assess_routes_escalated_dossier_to_outbox(
    monkeypatch,
    tmp_path,
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
    """A completed assessment enqueues exactly one review to the router's outbox (R8)."""
    monkeypatch.setenv("CDD_LOCAL_REVIEW_OUTBOX", str(tmp_path / "outbox.db"))
    router = LocalReviewRouter(Settings())
    service = _service_with_router(
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
        router,
    )
    assert not router.outbox.pending()

    case = service.assess(
        sample_cases.SAMPLE_CASE_INPUT,
        actor=ACTOR,
        tenant="demo-bank",
    )
    assert case.requires_human_review
    assert case.subject.tenant == "demo-bank"

    pending = router.outbox.pending()
    assert len(pending) == 1, "the escalated dossier must be routed to Hrz7 exactly once"
    review = pending[0].review
    assert review.action == f"cdd_dossier:{case.subject.type.value}"
    assert review.case_ref == case.id
    assert review.maker == ACTOR
    assert review.tenant == "demo-bank"
    assert review.source_key == f"doc1:{case.subject.tenant}:{case.id}:cdd_dossier"


def _high_risk_case_with_pii() -> CDDCase:
    subject = Subject(
        id="subj-acme",
        name="Acme Holdings (FICTIONAL)",
        type=SubjectType.ENTITY,
        jurisdiction="SG",
        tenant="demo-bank",
    )
    # A citation snippet carrying a synthetic SG NRIC: it must be masked before the wire.
    cite = Citation(
        source_id="doc-1",
        source_type=SourceType.DOCUMENT,
        title="Registry extract",
        snippet="Director NRIC S1234567D listed on the filing.",
    )
    sow = SourceOfWealthNarrative(subject_id=subject.id, narrative="Business proceeds.")
    rating = RiskRating(band=RiskBand.HIGH, score=0.9, citations=(cite,))
    return CDDCase(id="cdd-subj-acme", subject=subject, sow=sow, rating=rating)


def test_payload_is_redacted_and_carries_tenant_and_severity():
    """The wire payload masks identifiers, carries the tenant, and maps the risk band (R1/R8)."""
    review = case_to_review(_high_risk_case_with_pii(), maker=ACTOR)

    assert review.tenant == "demo-bank"
    assert review.severity == "high"
    assert review.required_approvals == 2, "a HIGH-risk dossier warrants dual control"
    # No raw NRIC survives into the payload the console receives.
    assert "S1234567D" not in review.summary
    for citation in review.citations:
        assert "S1234567D" not in citation.snippet
    assert any(c.title == "Registry extract" for c in review.citations)


def test_no_router_still_assembles_dossier(
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
    """Routing is optional: with no router bound, assessment still returns an escalated dossier."""
    service = _service_with_router(
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
        None,
    )
    case = service.assess(sample_cases.SAMPLE_CASE_INPUT, actor=ACTOR)
    assert case.requires_human_review


def _pkyc_assessment():
    """An urgent perpetual-KYC assessment carrying an obviously fictional identifier."""
    from datetime import date

    from cdd_sow_research.domain.entitlements import case_tags
    from cdd_sow_research.domain.models import (
        AdverseMediaFinding,
        QueuePriority,
        Severity,
    )
    from cdd_sow_research.domain.perpetual_kyc import PerpetualKycEngine

    engine = PerpetualKycEngine()
    subject = Subject(
        id="subj-acme",
        name="Acme Holdings Pte Ltd (FICTIONAL)",
        type=SubjectType.ENTITY,
        tenant="demo-bank",
    )
    finding = AdverseMediaFinding(
        headline="Fictional enquiry involving NRIC S1234567D",
        publisher="The Invented Times (FICTIONAL)",
        url="https://example.test/fictional",
        severity=Severity.HIGH,
        snippet="Fictional narrative mentioning NRIC S1234567D as a test fixture.",
    )
    first = engine.assess(subject=subject, as_of=date(2026, 8, 5))
    assessment = engine.assess(
        subject=subject,
        as_of=date(2026, 9, 1),
        baseline=engine.next_baseline(first),
        adverse_media=(finding,),
        acl=case_tags(subject.id, subject.tenant),
    )
    assert assessment.queue_item is not None
    assert assessment.queue_item.priority in {QueuePriority.HIGH, QueuePriority.URGENT}
    return assessment


def test_perpetual_kyc_assessment_is_redacted_before_the_wire():
    """R1: no raw identifier reaches Hrz7 from the perpetual-KYC hand-off either."""
    review = assessment_to_review(_pkyc_assessment(), maker=ACTOR)

    assert review.action == "perpetual_kyc_rescore"
    assert review.tenant == "demo-bank"
    assert review.required_approvals == 2, "a high-priority re-score warrants dual control"
    assert review.source_key.endswith(":pkyc:2026-09-01"), "retry must be idempotent per run"
    assert "S1234567D" not in review.summary
    assert "S1234567D" not in review.subject
    for citation in review.citations:
        assert "S1234567D" not in citation.snippet


def test_perpetual_kyc_routes_through_the_local_outbox(monkeypatch):
    """The local router persists the pKYC escalation exactly as it does a dossier."""
    monkeypatch.setenv("CDD_LOCAL_REVIEW_OUTBOX", ":memory:")
    monkeypatch.delenv("CDD_HRZ7_URL", raising=False)
    router = LocalReviewRouter(Settings())
    router.route_monitoring(_pkyc_assessment(), maker=ACTOR)

    pending = router.outbox.pending()
    assert len(pending) == 1
    assert pending[0].review.action == "perpetual_kyc_rescore"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
