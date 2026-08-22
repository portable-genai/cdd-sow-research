"""Perpetual KYC: the deterministic engine, its policy arithmetic and its queue.

These tests pin the properties the module is worth having:

* the whole assessment is REPLAYABLE (same inputs + same ``as_of`` => identical output);
* the numbers come from bank-owned policy, not from a model or a constant in code;
* the first run establishes a baseline instead of treating a standing picture as change;
* movement (a new sanctions hit, a changed shareholding) is what re-scores; and
* the outcome always requires human review and never auto-acts.

All data here is obviously fictional and asserts nothing about any real party.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from cdd_sow_research.domain.models import (
    AdverseMediaCategory,
    AdverseMediaFinding,
    BeneficialOwner,
    CddTier,
    HitStatus,
    ListSource,
    OwnershipSummary,
    QueuePriority,
    RiskBand,
    ScreeningAlert,
    ScreeningMatch,
    ScreeningResult,
    Severity,
    SignalChange,
    SignalSource,
    Subject,
    SubjectType,
    WatchlistEntry,
)
from cdd_sow_research.domain.perpetual_kyc import PerpetualKycEngine, signal_key
from cdd_sow_research.domain.policy import PerpetualKycPolicy

_AS_OF = date(2026, 8, 5)
_SUBJECT = Subject(
    id="subj-acme",
    name="Acme Holdings Pte Ltd (FICTIONAL)",
    type=SubjectType.ENTITY,
    jurisdiction="SG",
    tenant="demo-bank",
)


def _engine() -> PerpetualKycEngine:
    return PerpetualKycEngine.from_policy(PerpetualKycPolicy())


def _ownership(pct: float = 75.0, is_pep: bool = False) -> OwnershipSummary:
    return OwnershipSummary(
        root_entity=_SUBJECT.name,
        owners=(
            BeneficialOwner(
                name="Jordan Fictitious (FICTIONAL)", pct=pct, country="SG", is_pep=is_pep
            ),
        ),
    )


def _media(category: AdverseMediaCategory = AdverseMediaCategory.FRAUD) -> AdverseMediaFinding:
    return AdverseMediaFinding(
        headline="Fictional regulator opens enquiry into invented company",
        publisher="The Invented Times (FICTIONAL)",
        url="https://example.test/fictional-story",
        category=category,
        severity=Severity.HIGH,
        snippet="Entirely fictional reporting used only as a test fixture.",
    )


def _screening(status: HitStatus = HitStatus.PENDING) -> ScreeningResult:
    entry = WatchlistEntry(
        uid="FICTIONAL-0001",
        source=ListSource.OFAC_SDN,
        name="Invented Designated Party (FICTIONAL)",
        list_version="2026-08-01",
    )
    match = ScreeningMatch(
        entry=entry, score=0.94, matched_name=entry.name, features=("name 0.94",)
    )
    return ScreeningResult(
        subject_id=_SUBJECT.id,
        query_name=_SUBJECT.name,
        lists_version="2026-08-01",
        sources=(ListSource.OFAC_SDN,),
        alerts=(ScreeningAlert(id="alert-1", subject_id=_SUBJECT.id, match=match, status=status),),
    )


# --------------------------------------------------------------------------- #
# Fingerprints and determinism
# --------------------------------------------------------------------------- #
def test_signal_key_is_stable_and_normalising():
    a = signal_key(SignalSource.REGISTRY, "Acme  Holdings", "Jordan Fictitious", "75.00")
    b = signal_key(SignalSource.REGISTRY, "acme holdings", "jordan fictitious", "75.00")
    assert a == b, "cosmetic rendering differences must not look like a new signal"
    assert a.startswith("registry:")
    assert a != signal_key(SignalSource.REGISTRY, "Acme Holdings", "Jordan Fictitious", "60.00")


def test_assessment_is_replayable():
    engine = _engine()
    kwargs = dict(
        subject=_SUBJECT,
        as_of=_AS_OF,
        adverse_media=(_media(),),
        ownership=_ownership(),
    )
    first = engine.assess(**kwargs)
    second = engine.assess(**kwargs)
    assert first == second, "same inputs and same as_of must yield an identical assessment"
    assert first.generated_at == second.generated_at


# --------------------------------------------------------------------------- #
# Detection: first run vs movement
# --------------------------------------------------------------------------- #
def test_first_run_establishes_a_baseline_rather_than_reporting_change():
    engine = _engine()
    assessment = engine.assess(
        subject=_SUBJECT, as_of=_AS_OF, adverse_media=(_media(),), ownership=_ownership()
    )
    assert all(s.change is SignalChange.PERSISTING for s in assessment.signals)
    assert assessment.score_delta == 0.0
    assert not assessment.material
    baseline = engine.next_baseline(assessment)
    assert len(baseline.signal_keys) == len(assessment.signals)


def test_a_new_sanctions_hit_is_detected_scored_and_made_urgent():
    engine = _engine()
    first = engine.assess(subject=_SUBJECT, as_of=_AS_OF, ownership=_ownership())
    baseline = engine.next_baseline(first)

    second = engine.assess(
        subject=_SUBJECT,
        as_of=date(2026, 9, 1),
        baseline=baseline,
        screening=_screening(),
        ownership=_ownership(),
    )
    new = second.new_signals
    assert len(new) == 1
    assert new[0].source is SignalSource.SANCTIONS
    assert second.score > second.baseline_score
    assert second.band is RiskBand.PROHIBITED and second.tier is CddTier.EDD
    assert second.queue_item is not None
    assert second.queue_item.priority is QueuePriority.URGENT
    # SLA for URGENT is one day under the reference policy.
    assert second.queue_item.sla_due == "2026-09-02"
    assert second.requires_human_review is True
    assert second.queue_item.citations, "an urgent queue item must carry its evidence"


def test_a_changed_shareholding_clears_the_old_signal_and_raises_a_new_one():
    engine = _engine()
    first = engine.assess(subject=_SUBJECT, as_of=_AS_OF, ownership=_ownership(pct=75.0))
    baseline = engine.next_baseline(first)

    second = engine.assess(
        subject=_SUBJECT,
        as_of=date(2026, 9, 1),
        baseline=baseline,
        ownership=_ownership(pct=51.0),
    )
    changes = {s.change for s in second.signals}
    assert SignalChange.NEW in changes and SignalChange.CLEARED in changes
    assert second.material


def test_a_cleared_signal_relieves_only_part_of_its_uplift():
    engine = _engine()
    with_media = engine.assess(subject=_SUBJECT, as_of=_AS_OF, adverse_media=(_media(),))
    baseline = replace(engine.next_baseline(with_media), score=0.30)

    cleared = engine.assess(subject=_SUBJECT, as_of=date(2026, 9, 1), baseline=baseline)
    assert cleared.cleared_signals
    # HIGH media uplift 0.12 * source weight 0.8 = 0.096; relief is half of that.
    assert cleared.score == pytest.approx(0.30 - 0.048, abs=1e-4)
    assert cleared.score < baseline.score


# --------------------------------------------------------------------------- #
# Policy arithmetic (B4: the numbers live in config, not in code)
# --------------------------------------------------------------------------- #
def test_uplift_follows_the_configured_severity_and_source_weights():
    tuned = PerpetualKycEngine.from_policy(
        PerpetualKycPolicy(
            severity_uplift={"low": 0.0, "medium": 0.0, "high": 0.30, "critical": 0.30},
            source_weight={"sanctions": 1.0, "adverse_media": 1.0, "registry": 1.0},
        )
    )
    first = tuned.assess(subject=_SUBJECT, as_of=_AS_OF)
    second = tuned.assess(
        subject=_SUBJECT,
        as_of=date(2026, 9, 1),
        baseline=tuned.next_baseline(first),
        adverse_media=(_media(),),
    )
    assert second.score == pytest.approx(0.30, abs=1e-4)


def test_total_uplift_is_capped_by_policy():
    tuned = PerpetualKycEngine.from_policy(
        PerpetualKycPolicy(severity_uplift={"high": 0.4, "medium": 0.4}, max_uplift=0.10)
    )
    first = tuned.assess(subject=_SUBJECT, as_of=_AS_OF)
    findings = tuple(
        replace(_media(), url=f"https://example.test/fictional-{i}", headline=f"Fictional item {i}")
        for i in range(5)
    )
    second = tuned.assess(
        subject=_SUBJECT,
        as_of=date(2026, 9, 1),
        baseline=tuned.next_baseline(first),
        adverse_media=findings,
    )
    assert second.score == pytest.approx(0.10, abs=1e-4)


def test_sla_days_come_from_policy():
    tuned = PerpetualKycEngine.from_policy(
        PerpetualKycPolicy(sla_days={"urgent": 3, "high": 3, "standard": 3, "low": 3})
    )
    assessment = tuned.assess(subject=_SUBJECT, as_of=_AS_OF, ownership=_ownership())
    assert assessment.queue_item is not None
    assert assessment.queue_item.sla_due == "2026-08-08"


# --------------------------------------------------------------------------- #
# Explainability and the maker-checker gate
# --------------------------------------------------------------------------- #
def test_every_signal_has_an_uplift_line_and_every_finding_a_citation():
    engine = _engine()
    assessment = engine.assess(
        subject=_SUBJECT,
        as_of=_AS_OF,
        screening=_screening(HitStatus.TRUE_POSITIVE),
        adverse_media=(_media(AdverseMediaCategory.SANCTIONS),),
        ownership=_ownership(is_pep=True),
    )
    assert len(assessment.uplifts) == len(assessment.signals)
    assert {u.key for u in assessment.uplifts} == {s.key for s in assessment.signals}
    assert all(s.citation is not None for s in assessment.signals)
    assert assessment.citations


def test_an_outcome_always_requires_human_review():
    engine = _engine()
    quiet = engine.assess(subject=_SUBJECT, as_of=_AS_OF)
    assert quiet.requires_human_review is True
    assert quiet.queue_item is not None
    assert quiet.queue_item.requires_human_review is True
    assert quiet.queue_item.reasons, "a queue item must always explain itself"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
