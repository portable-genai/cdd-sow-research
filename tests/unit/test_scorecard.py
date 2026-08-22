"""Unit tests for country risk and the risk-based CDD scorecard + tiering."""

from __future__ import annotations

from cdd_sow_research.domain import country_risk as cr
from cdd_sow_research.domain.models import (
    AdverseMediaCategory,
    AdverseMediaFinding,
    CddTier,
    ListSource,
    RiskBand,
    ScreeningAlert,
    ScreeningMatch,
    ScreeningResult,
    Severity,
    Subject,
    SubjectType,
    WatchlistEntry,
)
from cdd_sow_research.domain.scorecard_service import RiskScorecardService

SVC = RiskScorecardService()


# --- country risk --------------------------------------------------------- #
def test_country_risk_levels() -> None:
    assert cr.country_risk("IR").level == "prohibited"
    assert cr.country_risk("kp").level == "prohibited"  # case-insensitive
    assert cr.country_risk("SY").level == "high"  # grey list
    assert cr.country_risk("SG").level == "low"
    assert cr.country_risk("").level == "medium"  # unknown


# --- scorecard ------------------------------------------------------------ #
def _subject(jur="SG", t=SubjectType.INDIVIDUAL):
    return Subject(id="s", name="Test", type=t, jurisdiction=jur)


def test_low_risk_individual_is_sdd() -> None:
    sc = SVC.score(_subject(), product="deposit", channel="branch")
    assert sc.band is RiskBand.LOW
    assert sc.tier is CddTier.SDD
    assert sc.hard_signals == ()


def test_pep_forces_edd_and_high() -> None:
    sc = SVC.score(_subject(), is_pep=True)
    assert sc.tier is CddTier.EDD
    assert sc.band is RiskBand.HIGH
    assert "politically-exposed person" in sc.hard_signals


def test_fatf_call_for_action_is_prohibited_edd() -> None:
    sc = SVC.score(_subject(jur="IR", t=SubjectType.ENTITY))
    assert sc.band is RiskBand.PROHIBITED
    assert sc.tier is CddTier.EDD
    assert "FATF call-for-action jurisdiction" in sc.hard_signals


def test_open_sanctions_hit_forces_prohibited_edd() -> None:
    entry = WatchlistEntry(uid="1", source=ListSource.OFAC_SDN, name="Test")
    alert = ScreeningAlert(
        id="a", subject_id="s", match=ScreeningMatch(entry=entry, score=1.0, matched_name="Test")
    )
    screening = ScreeningResult(subject_id="s", query_name="Test", alerts=(alert,))
    sc = SVC.score(_subject(), screening=screening)
    assert sc.band is RiskBand.PROHIBITED
    assert sc.tier is CddTier.EDD
    assert "open sanctions/watchlist hit" in sc.hard_signals


def test_pep_screening_hit_sets_pep_exposure() -> None:
    entry = WatchlistEntry(uid="p", source=ListSource.PEP, name="Test")
    alert = ScreeningAlert(
        id="a", subject_id="s", match=ScreeningMatch(entry=entry, score=1.0, matched_name="Test")
    )
    screening = ScreeningResult(subject_id="s", query_name="Test", alerts=(alert,))
    sc = SVC.score(_subject(), screening=screening)
    assert "politically-exposed person" in sc.hard_signals
    assert sc.tier is CddTier.EDD


def test_adverse_media_raises_score() -> None:
    media = (
        AdverseMediaFinding(
            headline="probe",
            publisher="x",
            url="",
            category=AdverseMediaCategory.FRAUD,
            severity=Severity.HIGH,
        ),
    )
    hi = SVC.score(_subject(), adverse_media=media)
    lo = SVC.score(_subject())
    assert hi.score > lo.score


def test_sanctions_terrorism_media_is_hard_signal() -> None:
    media = (
        AdverseMediaFinding(
            headline="x",
            publisher="y",
            url="",
            category=AdverseMediaCategory.TERRORISM,
            severity=Severity.CRITICAL,
        ),
    )
    sc = SVC.score(_subject(), adverse_media=media)
    assert sc.tier is CddTier.EDD
    assert "sanctions/terrorism adverse media" in sc.hard_signals


def test_factors_normalised_and_in_range() -> None:
    sc = SVC.score(_subject(jur="SY"), product="crypto", channel="non_face_to_face")
    assert 0.0 <= sc.score <= 1.0
    assert {f.name for f in sc.factors} >= {"geography", "customer_type", "product", "channel"}


def test_deterministic() -> None:
    a = SVC.score(_subject(jur="IR"), is_pep=True, product="crypto")
    b = SVC.score(_subject(jur="IR"), is_pep=True, product="crypto")
    assert (a.score, a.band, a.tier, a.hard_signals) == (b.score, b.band, b.tier, b.hard_signals)
