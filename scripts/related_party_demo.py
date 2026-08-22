"""Demo: one system for a company AND its key individuals (synthetic, fictional data).

Onboards a company (Acme Holdings) then runs CDD on its key individuals (UBOs >=25% +
directors) and a full SoW sub-case on the source-of-funds individuals, screens every
party, computes a risk scorecard + CDD tier, reconciles Source of Funds, and derives the
ongoing-monitoring outcome, rolling everything into the company case. A PEP UBO escalates
the parent to enhanced review (soft: a checker still disposes).

    PYTHONPATH=src python scripts/related_party_demo.py [out.json]

The scenario builders (``build_*``) are also imported by ``scripts/sow_demo_server.py`` so
the live walkthrough and this static export stay in lockstep. No cloud, no LLM: every
result is deterministic.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, date, datetime

import sow_demo as demo

from cdd_sow_research.adapters.local.case_store import InMemoryCaseStore
from cdd_sow_research.adapters.local.sanctions_provider import LocalSanctionsProviderAdapter
from cdd_sow_research.config import Settings
from cdd_sow_research.domain.models import (
    BeneficialOwner,
    Citation,
    DeclaredFunds,
    DeclaredSource,
    DocType,
    FundsDeclaration,
    FundsFlow,
    FundsOriginKind,
    MonitoringAssessment,
    OwnershipSummary,
    PartyRole,
    PartyScreening,
    RelatedParty,
    RelatedPartyReview,
    RiskScorecard,
    ScreeningResult,
    SourceOfFundsAssessment,
    SourceType,
    Subject,
    SubjectType,
    WealthDeclaration,
    WealthSourceKind,
)
from cdd_sow_research.domain.periodic_review_service import PeriodicReviewService
from cdd_sow_research.domain.policy import RiskPolicy
from cdd_sow_research.domain.related_party import RelatedPartyInput, RelatedPartyService
from cdd_sow_research.domain.scorecard_service import RiskScorecardService
from cdd_sow_research.domain.screening import ScreeningService
from cdd_sow_research.domain.serialization import to_jsonable
from cdd_sow_research.domain.source_of_funds_service import SourceOfFundsService
from cdd_sow_research.domain.sow_case_service import SowCaseService

K = WealthSourceKind
FK = FundsOriginKind
RM = demo.RM

COMPANY = Subject(
    id="acme-holdings",
    name="Acme Holdings Pte Ltd (FICTIONAL)",
    type=SubjectType.ENTITY,
    jurisdiction="SG",
)
PRINCIPALS = (f"case:{COMPANY.id}",)
_REGISTRY_CITE = Citation(
    source_id="acra-extract", source_type=SourceType.REGISTRY, title="ACRA company extract", page=1
)


def _run_individual_sow(svc: SowCaseService, person: Subject) -> tuple[str, object, float]:
    """Open + run a one-round SoW sub-case for a source-of-funds individual."""
    cid = f"sow-{person.id}"
    principals = (f"case:{cid}",)
    decl = WealthDeclaration(
        sources=(
            DeclaredSource(K.EMPLOYMENT, "Executive compensation", "USD 5m-10m"),
            DeclaredSource(K.INVESTMENTS, "Personal portfolio", "USD 5m-15m"),
        ),
        declared_net_worth_band="USD 10m-25m",
    )
    svc.open(cid, person, decl, actor=RM)
    items = [
        demo._evidence(
            f"{person.id}-emp",
            DocType.FIN_STATEMENT,
            K.EMPLOYMENT,
            "USD 5m-10m",
            "2026-02-10",
            "Employer compensation letter",
        ),
        demo._evidence(
            f"{person.id}-pf",
            DocType.FIN_STATEMENT,
            K.INVESTMENTS,
            "USD 5m-15m",
            "2026-02-20",
            "Brokerage statement 2026",
        ),
    ]
    svc.add_evidence(cid, principals, items, actor=RM)
    case = svc.analyze(cid, principals, actor=RM, as_of=datetime(2026, 3, 5, tzinfo=UTC))
    return cid, case.status, case.current.reconciliation.coverage_pct


# --------------------------------------------------------------------------- #
# Scenario builders (shared by the static export AND the live demo server).
# --------------------------------------------------------------------------- #
def build_related_party_review(svc: SowCaseService) -> RelatedPartyReview:
    """Beneficial ownership -> per-party CDD + SoW sub-cases -> rolled-up review."""
    ownership = OwnershipSummary(
        root_entity=COMPANY.name,
        owners=(
            BeneficialOwner(
                name="Tan Wei Ming",
                pct=60.0,
                country="SG",
                is_pep=True,
                citations=(_REGISTRY_CITE,),
            ),
            BeneficialOwner(
                name="Lim Mei Ling", pct=30.0, country="SG", citations=(_REGISTRY_CITE,)
            ),
            BeneficialOwner(
                name="Junior Holder", pct=5.0, country="SG", citations=(_REGISTRY_CITE,)
            ),
        ),
    )
    rps = RelatedPartyService()
    parties = list(rps.derive_from_ownership(ownership))
    parties.append(
        RelatedParty(
            id="rp-ong-cfo",
            subject=Subject(
                id="ong-boon", name="Ong Boon (CFO)", type=SubjectType.INDIVIDUAL, jurisdiction="SG"
            ),
            role=PartyRole.DIRECTOR,
            citations=(_REGISTRY_CITE,),
        )
    )
    entries: list[RelatedPartyInput] = []
    for p in parties:
        screening = PartyScreening(identity_verified=True, is_pep=p.subject.name == "Tan Wei Ming")
        sow_id = sow_status = None
        sow_cov = 0.0
        if rps.in_scope(p) and p.source_of_funds:
            sow_id, sow_status, sow_cov = _run_individual_sow(svc, p.subject)
        entries.append(
            RelatedPartyInput(
                p, screening, sow_case_id=sow_id, sow_status=sow_status, sow_coverage_pct=sow_cov
            )
        )
    return rps.assess(entries)


def build_screening(review: RelatedPartyReview) -> ScreeningResult:
    """Screen the company + its in-scope key individuals against the synced snapshot."""
    provider = LocalSanctionsProviderAdapter(Settings())
    screener = ScreeningService()
    screened = [COMPANY, *[o.party.subject for o in review.in_scope]]
    all_alerts: list = []
    all_sources: set = set()
    for subj in screened:
        res = screener.screen_subject(subj, provider)
        all_alerts.extend(res.alerts)
        all_sources.update(res.sources)
    return ScreeningResult(
        subject_id=COMPANY.id,
        query_name=COMPANY.name,
        lists_version=provider.version(),
        sources=tuple(sorted(all_sources, key=lambda s: s.value)),
        alerts=tuple(all_alerts),
    )


def build_scorecard(screening: ScreeningResult, policy: RiskPolicy | None = None) -> RiskScorecard:
    policy = policy or RiskPolicy()
    service = RiskScorecardService.from_policy(policy.scorecard, policy.country_risk)
    return service.score(
        COMPANY, screening=screening, is_pep=True, product="private_banking", channel="introduced"
    )


def build_source_of_funds(policy: RiskPolicy | None = None) -> SourceOfFundsAssessment:
    sof_cite = Citation(
        source_id="dbs-credit-advice",
        source_type=SourceType.DOCUMENT,
        title="Bank credit advice",
        page=1,
    )
    declaration = FundsDeclaration(
        sources=(
            DeclaredFunds(FK.BUSINESS_INCOME, "Dividends from Acme operating co", "USD 8m-12m"),
            DeclaredFunds(FK.ASSET_SALE, "Sale of warehouse property", "USD 15m-20m"),
        ),
        expected_inflow_band="USD 25m-30m",
        expected_activity="Quarterly dividend inflows plus a one-off property disposal in 2026.",
    )
    flows = [
        FundsFlow(
            id="flow-dividend-q1",
            kind=FK.BUSINESS_INCOME,
            description="Q1 dividend from operating company",
            amount_band="USD 8m-12m",
            value_date="2026-03-01",
            citations=(sof_cite,),
        ),
        FundsFlow(
            id="flow-gift",
            kind=FK.GIFT,
            description="Inbound transfer from a third party",
            amount_band="USD 5m-8m",
            value_date="2026-03-18",
            citations=(sof_cite,),
        ),
    ]
    policy = policy or RiskPolicy()
    return SourceOfFundsService.from_policy(policy.source_of_funds).assess(
        COMPANY.id, declaration, flows
    )


def build_monitoring(
    scorecard: RiskScorecard,
    screening: ScreeningResult,
    sof: SourceOfFundsAssessment,
    policy: RiskPolicy | None = None,
) -> MonitoringAssessment:
    policy = policy or RiskPolicy()
    reviewer = PeriodicReviewService.from_policy(policy.monitoring)
    triggers = reviewer.triggers_from_signals(screening=screening, source_of_funds=sof, is_pep=True)
    return reviewer.assess(
        COMPANY.id,
        scorecard=scorecard,
        last_reviewed="2025-03-05",
        as_of=date(2026, 6, 23),
        triggers=triggers,
    )


def main(out_path: str) -> None:
    store = InMemoryCaseStore()
    # Bank-owned policy comes from settings (config/settings.yaml `policy:`), threaded
    # into every deterministic engine on the SoW-case path.
    policy = Settings.load().policy
    svc = SowCaseService.from_policy(store, policy)

    # 1) The company SoW case — run it to ready using the existing Acme scenario.
    svc.open(COMPANY.id, COMPANY, demo._declaration(), actor=RM)
    company_case = None
    for as_of, items in demo.ROUNDS:
        svc.add_evidence(COMPANY.id, PRINCIPALS, items, actor=RM)
        company_case = svc.analyze(COMPANY.id, PRINCIPALS, actor=RM, as_of=as_of)
    print(
        f"Company case {COMPANY.id}: {company_case.status.value} "
        f"({round(company_case.current.reconciliation.coverage_pct * 100)}% coverage)\n"
    )

    review = build_related_party_review(svc)
    print(f"Key individuals: {review.summary}  escalated={review.escalated}")
    svc.attach_related_parties(COMPANY.id, PRINCIPALS, review, actor=RM)

    screening = build_screening(review)
    print(f"Screening: {len(screening.open_alerts)} open hit(s)")
    svc.attach_screening(COMPANY.id, PRINCIPALS, screening, actor=RM)

    scorecard = build_scorecard(screening, policy)
    print(
        f"Scorecard: {round(scorecard.score * 100)}% → {scorecard.band.value.upper()} "
        f"/ {scorecard.tier.value.upper()}  signals={list(scorecard.hard_signals)}"
    )
    svc.attach_scorecard(COMPANY.id, PRINCIPALS, scorecard, actor=RM)

    sof = build_source_of_funds(policy)
    print(f"Source of Funds: {round(sof.coverage_pct * 100)}% coverage, {len(sof.gaps)} gap(s)")
    svc.attach_source_of_funds(COMPANY.id, PRINCIPALS, sof, actor=RM)

    monitoring = build_monitoring(scorecard, screening, sof, policy)
    print(
        f"Monitoring: {monitoring.review_status.value.upper()} · {len(monitoring.triggers)} trigger(s)"
    )
    final = svc.attach_monitoring(COMPANY.id, PRINCIPALS, monitoring, actor=RM)

    payload = {
        "case_id": COMPANY.id,
        "subject": to_jsonable(COMPANY),
        "final_status": final.status.value,
        "snapshot": None,
        "iterations": [
            {
                "no": final.iterations[-1].no,
                "as_of": demo.ROUNDS[-1][0].date().isoformat(),
                "status_after": final.status.value,
                "added_evidence": [],
                "audit_view": to_jsonable(final.current),
            }
        ],
    }
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    print(f"\nWrote company + key-individuals audit view -> {out_path}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "related_party_demo.json")
