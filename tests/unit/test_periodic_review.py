"""Unit tests for the deterministic ongoing-monitoring / periodic-review service."""

from __future__ import annotations

from datetime import date

from cdd_sow_research.domain.models import (
    CddTier,
    FundsGap,
    FundsGapKind,
    ListSource,
    ReviewStatus,
    ReviewTrigger,
    ReviewTriggerKind,
    RiskBand,
    RiskScorecard,
    ScreeningAlert,
    ScreeningMatch,
    ScreeningResult,
    Severity,
    SourceOfFundsAssessment,
    WatchlistEntry,
)
from cdd_sow_research.domain.periodic_review_service import PeriodicReviewService

SVC = PeriodicReviewService()


def _scorecard(tier: CddTier) -> RiskScorecard:
    band = {CddTier.SDD: RiskBand.LOW, CddTier.CDD: RiskBand.MEDIUM, CddTier.EDD: RiskBand.HIGH}[
        tier
    ]
    return RiskScorecard(score=0.5, band=band, tier=tier)


def test_edd_cadence_is_annual() -> None:
    a = SVC.assess("s", _scorecard(CddTier.EDD), "2026-01-01", date(2026, 6, 1))
    assert a.cadence_months == 12
    assert a.next_review_due == "2027-01-01"
    assert a.review_status is ReviewStatus.CURRENT


def test_sdd_cadence_is_five_years() -> None:
    a = SVC.assess("s", _scorecard(CddTier.SDD), "2026-01-01", date(2026, 6, 1))
    assert a.cadence_months == 60
    assert a.next_review_due == "2031-01-01"


def test_due_soon_within_window() -> None:
    # EDD reviewed a year ago minus ~30 days -> due in ~30 days -> due soon.
    a = SVC.assess("s", _scorecard(CddTier.EDD), "2025-07-01", date(2026, 6, 15))
    assert a.review_status is ReviewStatus.DUE_SOON
    assert 0 <= a.days_until_due <= SVC.due_soon_days


def test_overdue_escalates_and_synthesises_trigger() -> None:
    a = SVC.assess("s", _scorecard(CddTier.EDD), "2024-01-01", date(2026, 6, 1))
    assert a.review_status is ReviewStatus.OVERDUE
    assert a.days_until_due < 0
    assert a.escalates is True
    assert any(t.kind is ReviewTriggerKind.PERIODIC_DUE for t in a.triggers)


def test_never_reviewed_is_overdue_and_escalates() -> None:
    a = SVC.assess("s", _scorecard(CddTier.CDD), "", date(2026, 6, 1))
    assert a.review_status is ReviewStatus.OVERDUE
    assert a.escalates is True
    assert a.last_reviewed == ""
    assert any(t.kind is ReviewTriggerKind.PERIODIC_DUE for t in a.triggers)
    assert any("No prior periodic review" in n for n in a.notes)


def test_due_today_boundary_is_due_soon() -> None:
    # next_review_due == as_of -> days_until_due == 0 -> due soon, not overdue.
    a = SVC.assess("s", _scorecard(CddTier.EDD), "2025-06-01", date(2026, 6, 1))
    assert a.days_until_due == 0
    assert a.review_status is ReviewStatus.DUE_SOON


def test_caller_periodic_due_not_duplicated_when_overdue() -> None:
    caller = ReviewTrigger(
        kind=ReviewTriggerKind.PERIODIC_DUE,
        severity=Severity.LOW,
        summary="caller-supplied periodic due",
    )
    a = SVC.assess("s", _scorecard(CddTier.EDD), "2024-01-01", date(2026, 6, 1), triggers=[caller])
    periodic = [t for t in a.triggers if t.kind is ReviewTriggerKind.PERIODIC_DUE]
    assert len(periodic) == 1  # synthetic one not added on top of the caller's
    assert periodic[0].summary == "caller-supplied periodic due"


def test_overdue_wins_over_triggered_and_keeps_event_trigger() -> None:
    event = ReviewTrigger(
        kind=ReviewTriggerKind.OWNERSHIP_CHANGE, severity=Severity.HIGH, summary="UBO changed"
    )
    a = SVC.assess("s", _scorecard(CddTier.EDD), "2024-01-01", date(2026, 6, 1), triggers=[event])
    assert a.review_status is ReviewStatus.OVERDUE  # overdue takes precedence over triggered
    kinds = {t.kind for t in a.triggers}
    assert ReviewTriggerKind.PERIODIC_DUE in kinds
    assert ReviewTriggerKind.OWNERSHIP_CHANGE in kinds


def test_no_scorecard_defaults_to_cdd_cadence() -> None:
    a = SVC.assess("s", None, "2026-01-01", date(2026, 6, 1))
    assert a.tier is CddTier.CDD
    assert a.cadence_months == 36


def test_event_trigger_sets_triggered_status() -> None:
    trig = ReviewTrigger(
        kind=ReviewTriggerKind.OWNERSHIP_CHANGE,
        severity=Severity.HIGH,
        summary="UBO changed",
    )
    a = SVC.assess("s", _scorecard(CddTier.CDD), "2026-01-01", date(2026, 6, 1), triggers=[trig])
    assert a.review_status is ReviewStatus.TRIGGERED
    assert a.escalates is True


def test_current_when_no_triggers_and_far_from_due() -> None:
    a = SVC.assess("s", _scorecard(CddTier.CDD), "2026-01-01", date(2026, 6, 1))
    assert a.review_status is ReviewStatus.CURRENT
    assert a.escalates is False


def test_triggers_from_signals_sanctions_and_sof() -> None:
    entry = WatchlistEntry(uid="e1", source=ListSource.OFAC_SDN, name="Tan Wei Ming")
    alert = ScreeningAlert(
        id="a1",
        subject_id="s",
        match=ScreeningMatch(entry=entry, score=1.0, matched_name="Tan Wei Ming"),
    )
    screening = ScreeningResult(
        subject_id="s", query_name="Tan Wei Ming", lists_version="v1", alerts=(alert,)
    )
    sof = SourceOfFundsAssessment(
        subject_id="s",
        gaps=(
            FundsGap(
                id="g1",
                kind=FundsGapKind.UNEXPECTED_INFLOW,
                severity=Severity.HIGH,
                summary="gift",
            ),
        ),
    )
    trigs = SVC.triggers_from_signals(screening=screening, source_of_funds=sof, is_pep=True)
    kinds = {t.kind for t in trigs}
    assert ReviewTriggerKind.SANCTIONS_HIT in kinds
    assert ReviewTriggerKind.PEP_STATUS in kinds
    assert ReviewTriggerKind.UNUSUAL_ACTIVITY in kinds
    # Ranked: HIGH severities precede MEDIUM (PEP).
    sev_order = [t.severity for t in trigs]
    assert sev_order[-1] is Severity.MEDIUM


def test_month_clamp_on_short_month() -> None:
    a = SVC.assess("s", _scorecard(CddTier.EDD), "2025-01-31", date(2025, 6, 1))
    # 12 months from 2025-01-31 -> 2026-01-31 (no clamp needed, but exercises arithmetic).
    assert a.next_review_due == "2026-01-31"
    b = SVC.assess("s", _scorecard(CddTier.CDD), "2024-11-30", date(2025, 1, 1))
    # 36 months -> 2027-11-30
    assert b.next_review_due == "2027-11-30"


def test_deterministic() -> None:
    args = ("s", _scorecard(CddTier.EDD), "2024-01-01", date(2026, 6, 1))
    a = SVC.assess(*args)
    b = SVC.assess(*args)
    assert [(t.kind, t.severity, t.summary) for t in a.triggers] == [
        (t.kind, t.severity, t.summary) for t in b.triggers
    ]
    assert a.next_review_due == b.next_review_due
    assert a.review_status == b.review_status
