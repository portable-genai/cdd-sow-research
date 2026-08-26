"""Unit tests for the four CDD sub-services.

* SourceOfWealthService.build(subject, passages, actor) -> SourceOfWealthNarrative
* RiskRatingService.rate(subject, sow, adverse_media, ownership, passages, actor)
* AdverseMediaService.scan(subject, actor)              -> AdverseMediaScreening | None
* OwnershipService.resolve(subject, actor)               -> OwnershipSummary

Each is pure and driven by in-memory fakes (no Google Cloud SDK).
"""

from __future__ import annotations

import pytest
from tests.conftest import FakeAdverseMedia, FakeRegistry, FakeTracer, load_service
from tests.fixtures import sample_cases

from cdd_sow_research.domain.models import (
    AdverseMediaCategory,
    AdverseMediaFinding,
    OwnershipSummary,
    RiskBand,
    Severity,
    SourceOfWealthNarrative,
    WealthSource,
)

ACTOR = "analyst@bank.test"
VALID_SOURCE_IDS = {p.citation.source_id for p in sample_cases.SAMPLE_PASSAGES}


# --------------------------------------------------------------------------- #
# SourceOfWealthService
# --------------------------------------------------------------------------- #
def test_sow_is_grounded_and_cited(llm, tracer):
    service = load_service("SourceOfWealthService")(llm=llm, tracer=tracer)
    sow = service.build(
        sample_cases.ENTITY_SUBJECT, list(sample_cases.SAMPLE_PASSAGES), actor=ACTOR
    )

    assert isinstance(sow, SourceOfWealthNarrative)
    assert sow.narrative
    assert sow.requires_human_review is True
    assert sow.sources, "the narrative must break wealth into sources"
    for src in sow.sources:
        assert isinstance(src, WealthSource)
        assert src.description
        for c in src.citations:
            assert c.source_id in VALID_SOURCE_IDS
            assert c.page is not None


def test_sow_runs_self_critique_second_pass(llm, tracer):
    service = load_service("SourceOfWealthService")(llm=llm, tracer=tracer)
    service.build(sample_cases.ENTITY_SUBJECT, list(sample_cases.SAMPLE_PASSAGES), actor=ACTOR)
    # Two LLM calls: the synthesis and the groundedness self-critique.
    assert len(llm.requests) == 2


# --------------------------------------------------------------------------- #
# RiskRatingService
# --------------------------------------------------------------------------- #
def _sow() -> SourceOfWealthNarrative:
    return SourceOfWealthNarrative(
        subject_id=sample_cases.ENTITY_SUBJECT.id,
        narrative="Wealth from business ownership.",
        sources=(),
        citations=(),
        confidence=0.8,
    )


def test_risk_rating_is_well_formed(llm, tracer):
    service = load_service("RiskRatingService")(llm=llm, tracer=tracer)
    rating = service.rate(
        sample_cases.ENTITY_SUBJECT,
        _sow(),
        (),
        sample_cases.SAMPLE_OWNERSHIP,
        list(sample_cases.SAMPLE_PASSAGES),
        actor=ACTOR,
    )
    assert isinstance(rating.band, RiskBand)
    assert rating.factors
    assert rating.rationale
    assert rating.requires_human_review is True


def test_terrorism_hit_forces_prohibited(llm, tracer):
    service = load_service("RiskRatingService")(llm=llm, tracer=tracer)
    media = (
        AdverseMediaFinding(
            headline="Alleged terrorism financing link (FICTIONAL)",
            publisher="Example Wire",
            url="https://example.test/news/terror",
            category=AdverseMediaCategory.TERRORISM,
            severity=Severity.CRITICAL,
        ),
    )
    rating = service.rate(
        sample_cases.ENTITY_SUBJECT,
        _sow(),
        media,
        None,
        list(sample_cases.SAMPLE_PASSAGES),
        actor=ACTOR,
    )
    assert rating.band is RiskBand.PROHIBITED


def test_ordinary_adverse_media_raises_to_high(llm, tracer):
    service = load_service("RiskRatingService")(llm=llm, tracer=tracer)
    media = (
        AdverseMediaFinding(
            headline="Civil fraud allegation (FICTIONAL)",
            publisher="Example Wire",
            url="https://example.test/news/fraud",
            category=AdverseMediaCategory.FRAUD,
            severity=Severity.HIGH,
        ),
    )
    rating = service.rate(
        sample_cases.ENTITY_SUBJECT,
        _sow(),
        media,
        None,
        list(sample_cases.SAMPLE_PASSAGES),
        actor=ACTOR,
    )
    # Fake LLM returns medium; an adverse-media hit raises it to at least HIGH.
    assert rating.band in (RiskBand.HIGH, RiskBand.PROHIBITED)


# --------------------------------------------------------------------------- #
# AdverseMediaService
# --------------------------------------------------------------------------- #
def test_adverse_media_orders_by_severity():
    # Each headline names the subject: a finding that does not is discarded before ordering,
    # which is a different property with its own test.
    named = "Acme Holdings Pte Ltd (FICTIONAL)"
    findings = (
        AdverseMediaFinding(
            headline=f"{named} low", publisher="p", url="u1", severity=Severity.LOW
        ),
        AdverseMediaFinding(
            headline=f"{named} critical", publisher="p", url="u2", severity=Severity.CRITICAL
        ),
        AdverseMediaFinding(
            headline=f"{named} medium", publisher="p", url="u3", severity=Severity.MEDIUM
        ),
    )
    service = load_service("AdverseMediaService")(
        adverse_media=FakeAdverseMedia(findings=findings), tracer=FakeTracer()
    )
    out = service.scan(sample_cases.ENTITY_SUBJECT, actor=ACTOR)
    assert out is not None
    assert [f.severity for f in out.findings] == [
        Severity.CRITICAL,
        Severity.MEDIUM,
        Severity.LOW,
    ]
    # Every finding gets a MEDIA citation synthesised if it lacked one.
    assert all(f.citation is not None for f in out.findings)


def test_adverse_media_searched_and_clear_is_a_screening_not_nothing():
    """A backend that ran and found nothing returns a screening with no findings."""
    service = load_service("AdverseMediaService")(
        adverse_media=FakeAdverseMedia(findings=()), tracer=FakeTracer()
    )
    out = service.scan(sample_cases.ENTITY_SUBJECT, actor=ACTOR)
    assert out is not None
    assert out.findings == ()


def test_adverse_media_never_searched_is_none_not_an_empty_screening():
    """No reachable backend is not a clean result, and must not render as one."""
    service = load_service("AdverseMediaService")(
        adverse_media=FakeAdverseMedia(searched=False), tracer=FakeTracer()
    )
    assert service.scan(sample_cases.ENTITY_SUBJECT, actor=ACTOR) is None


# --------------------------------------------------------------------------- #
# OwnershipService
# --------------------------------------------------------------------------- #
def test_ownership_resolved_for_entity():
    service = load_service("OwnershipService")(registry=FakeRegistry(), tracer=FakeTracer())
    summary = service.resolve(sample_cases.ENTITY_SUBJECT, actor=ACTOR)
    assert isinstance(summary, OwnershipSummary)
    assert summary.owners


def test_ownership_empty_for_individual():
    service = load_service("OwnershipService")(registry=FakeRegistry(), tracer=FakeTracer())
    summary = service.resolve(sample_cases.INDIVIDUAL_SUBJECT, actor=ACTOR)
    assert summary.owners == ()
    assert summary.root_entity == sample_cases.INDIVIDUAL_SUBJECT.name


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
