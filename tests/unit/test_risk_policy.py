"""Bank-owned risk policy: defaults reproduce the historical constants, overrides bite.

Phase A of the common-base work externalised every policy number (tolerances, weights,
cadences, country lists, escalation bands) into ``domain/policy.py`` + the settings
``policy:`` section. These tests prove three things:

1. the dataclass defaults and the shipped ``config/settings.yaml`` section reproduce the
   reference behavior exactly (no silent drift on upgrade);
2. an override actually changes engine behavior (policy is live, not decorative); and
3. the engines accept taxonomy kinds OUTSIDE the reference enums (a deployment can
   extend the vocabulary through data, without editing engine code).
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from cdd_sow_research.config import Settings
from cdd_sow_research.domain.country_risk import country_risk
from cdd_sow_research.domain.gap_analysis import GapAnalysisService
from cdd_sow_research.domain.models import (
    AdverseMediaCategory,
    AdverseMediaFinding,
    CddTier,
    Citation,
    DeclaredSource,
    DocType,
    EvidenceItem,
    GapKind,
    KycDocument,
    RiskBand,
    SourceType,
    SowCase,
    Subject,
    SubjectType,
    WealthDeclaration,
    WealthSourceKind,
)
from cdd_sow_research.domain.periodic_review_service import PeriodicReviewService
from cdd_sow_research.domain.policy import (
    CountryRiskPolicy,
    EscalationPolicy,
    GapPolicy,
    MonitoringPolicy,
    RiskPolicy,
    ScorecardPolicy,
    UboGraphPolicy,
)
from cdd_sow_research.domain.review_policy import CddReviewPolicy
from cdd_sow_research.domain.scorecard_service import RiskScorecardService

K = WealthSourceKind
AS_OF = datetime(2026, 3, 1, tzinfo=UTC)


def _ev(eid: str, kind: str, band: str) -> EvidenceItem:
    return EvidenceItem(
        id=eid,
        document=KycDocument(id=eid, doc_type=DocType.BANK_STATEMENT),
        supports_kinds=(kind,),
        evidenced_band=band,
        doc_date="2026-02-01",
        citations=(Citation(source_id=eid, source_type=SourceType.DOCUMENT, title=eid, page=1),),
    )


def _case(kind: str, declared: str, evidenced: str) -> SowCase:
    return SowCase(
        id="acme",
        subject=Subject(id="acme", name="Acme", type=SubjectType.ENTITY, jurisdiction="SG"),
        declaration=WealthDeclaration(
            sources=(DeclaredSource(kind, "src", declared),),
            declared_net_worth_band=declared,
        ),
        ledger=(_ev("e1", kind, evidenced),),
    )


# --------------------------------------------------------------------------- #
# 1. Defaults reproduce the historical constants
# --------------------------------------------------------------------------- #
def test_policy_defaults_match_engine_defaults() -> None:
    policy = RiskPolicy()
    gap_default = GapAnalysisService()
    gap_from_policy = GapAnalysisService.from_policy(policy.gap)
    assert gap_from_policy.delta_tolerance == gap_default.delta_tolerance == 0.15
    assert gap_from_policy.stale_days == gap_default.stale_days == 180
    assert dict(gap_from_policy.mandatory_docs) == dict(gap_default.mandatory_docs)
    assert gap_from_policy.stale_doc_types == gap_default.stale_doc_types

    sc_default = RiskScorecardService()
    sc_from_policy = RiskScorecardService.from_policy(policy.scorecard, policy.country_risk)
    assert dict(sc_from_policy.weights) == dict(sc_default.weights)
    assert dict(sc_from_policy.product_risk) == dict(sc_default.product_risk)
    assert (sc_from_policy.edd_score, sc_from_policy.sdd_score) == (0.6, 0.25)

    pr_default = PeriodicReviewService()
    pr_from_policy = PeriodicReviewService.from_policy(policy.monitoring)
    assert pr_from_policy.cadence_months == pr_default.cadence_months
    assert pr_from_policy.cadence_months[CddTier.EDD] == 12


def test_settings_yaml_policy_section_reproduces_defaults() -> None:
    """The shipped settings.yaml spells the defaults out; parsing must round-trip."""
    settings = Settings.load("config/settings.yaml")
    assert settings.policy.gap == GapPolicy()
    assert settings.policy.scorecard == ScorecardPolicy()
    assert settings.policy.country_risk == CountryRiskPolicy()
    assert settings.policy.monitoring == MonitoringPolicy()
    assert settings.policy.escalation == EscalationPolicy()
    # The UBO opacity-severity bands and dual-control cut-off are now spelled out in the
    # yaml too, so no review threshold is left defaulting in code (the SPEC B4 claim).
    assert settings.policy.ubo_graph == UboGraphPolicy()
    assert settings.policy.ubo_graph.opacity_severity_bands == (
        (0.75, "critical"),
        (0.50, "high"),
        (0.25, "medium"),
    )
    assert settings.policy.ubo_graph.dual_control_opacity == 0.50


def test_ubo_opacity_bands_and_dual_control_are_adopter_owned() -> None:
    """A retuned settings ladder changes both the review severity and the four-eyes gate,
    proving the numbers are policy, not constants baked into the engine or the adapter."""
    retuned = UboGraphPolicy.from_mapping(
        {
            "opacity_severity_bands": [[0.90, "critical"], [0.40, "high"]],
            "dual_control_opacity": 0.30,
        }
    )
    assert retuned.opacity_severity_bands == ((0.90, "critical"), (0.40, "high"))
    assert retuned.dual_control_opacity == 0.30

    # A malformed override never yields a half-configured ladder: it falls back whole.
    assert (
        UboGraphPolicy.from_mapping({"opacity_severity_bands": [[0.5]]}).opacity_severity_bands
        == UboGraphPolicy().opacity_severity_bands
    )


# --------------------------------------------------------------------------- #
# 2. Overrides change behavior
# --------------------------------------------------------------------------- #
def test_gap_tolerance_override_flips_unreconciled_gap() -> None:
    # ~70% coverage: below the default 85% bar, above a loosened 60% bar.
    case = _case(K.EMPLOYMENT, "USD 10m-10m", "USD 7m-7m")
    strict = GapAnalysisService.from_policy(GapPolicy())
    loose = GapAnalysisService.from_policy(GapPolicy(delta_tolerance=0.40))
    assert any(g.kind is GapKind.UNRECONCILED_DELTA for g in strict.analyze(case, as_of=AS_OF).gaps)
    assert not any(
        g.kind is GapKind.UNRECONCILED_DELTA for g in loose.analyze(case, as_of=AS_OF).gaps
    )


def test_scorecard_threshold_override_changes_tier() -> None:
    subject = Subject(id="s1", name="P1", type=SubjectType.ENTITY, jurisdiction="SG")
    default_card = RiskScorecardService().score(subject, product="private_banking")
    forced_edd = RiskScorecardService.from_policy(ScorecardPolicy(edd_score=0.29)).score(
        subject, product="private_banking"
    )
    assert default_card.tier is not CddTier.EDD
    assert forced_edd.tier is CddTier.EDD


def test_country_policy_override_prohibits_new_jurisdiction() -> None:
    assert country_risk("SG").level == "low"
    custom = CountryRiskPolicy(call_for_action=frozenset({"SG"}))
    assert country_risk("SG", custom).level == "prohibited"


def test_monitoring_cadence_override_changes_due_date() -> None:
    reviewer = PeriodicReviewService.from_policy(
        MonitoringPolicy(cadence_months={"sdd": 60, "cdd": 6, "edd": 12})
    )
    outcome = reviewer.assess("s1", None, "2026-01-01", as_of=date(2026, 3, 1))
    assert outcome.next_review_due == "2026-07-01"
    assert outcome.cadence_months == 6


def test_escalation_policy_override_changes_review_gate() -> None:
    media = (
        AdverseMediaFinding(
            headline="h", publisher="p", url="u", category=AdverseMediaCategory.FRAUD
        ),
    )
    default_gate = CddReviewPolicy()
    fraud_gate = CddReviewPolicy.from_policy(
        EscalationPolicy(escalating_media=frozenset({"fraud"}))
    )
    assert default_gate.escalates(RiskBand.LOW, media) is False
    assert fraud_gate.escalates(RiskBand.LOW, media) is True
    # The always-on maker-checker flag is not configurable (P-06).
    assert fraud_gate.requires_review() is True


# --------------------------------------------------------------------------- #
# 3. The taxonomy axis is open: engines accept kinds outside the reference enums
# --------------------------------------------------------------------------- #
def test_engine_accepts_extended_taxonomy_kind() -> None:
    case = _case("royalties", "USD 5m-5m", "USD 5m-5m")
    result = GapAnalysisService().analyze(case, as_of=AS_OF)
    assert [g.kind for g in result.groups] == ["royalties"]
    assert result.reconciliation.lines[0].kind == "royalties"
    # A policy table can require documents for the extended kind, too.
    demanding = GapAnalysisService.from_policy(
        GapPolicy(mandatory_docs={"royalties": frozenset({"fin_statement"})})
    )
    gaps = demanding.analyze(case, as_of=AS_OF).gaps
    mandatory = [g for g in gaps if g.kind is GapKind.MISSING_MANDATORY_DOC]
    assert mandatory and mandatory[0].related_kind == "royalties"
