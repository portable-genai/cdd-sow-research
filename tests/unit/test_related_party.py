"""Unit tests for the related-party (key individuals) derivation and roll-up."""

from __future__ import annotations

from cdd_sow_research.domain.models import (
    BeneficialOwner,
    CaseStatus,
    OwnershipSummary,
    PartyRole,
    PartyScreening,
    RelatedParty,
    Severity,
    Subject,
    SubjectType,
)
from cdd_sow_research.domain.related_party import (
    RelatedPartyInput,
    RelatedPartyPolicy,
    RelatedPartyService,
)

SVC = RelatedPartyService()


def _party(name: str, role: PartyRole, pct: float = 0.0, sof: bool = False) -> RelatedParty:
    return RelatedParty(
        id=f"rp-{name}",
        subject=Subject(id=name, name=name, type=SubjectType.INDIVIDUAL),
        role=role,
        pct=pct,
        source_of_funds=sof,
    )


# --- derivation ----------------------------------------------------------- #
def test_derive_from_ownership_maps_owners_and_threshold() -> None:
    own = OwnershipSummary(
        root_entity="Acme",
        owners=(
            BeneficialOwner(name="Big Owner", pct=60.0, country="SG", is_pep=True),
            BeneficialOwner(name="Small Owner", pct=5.0, country="SG"),
        ),
    )
    parties = SVC.derive_from_ownership(own)
    assert [p.subject.name for p in parties] == ["Big Owner", "Small Owner"]
    big, small = parties
    assert big.role is PartyRole.BENEFICIAL_OWNER
    assert big.pct == 60.0
    assert big.source_of_funds is True  # >= 25% -> source of funds
    assert small.source_of_funds is False
    assert big.subject.type is SubjectType.INDIVIDUAL


def test_derive_from_none_is_empty() -> None:
    assert SVC.derive_from_ownership(None) == ()


# --- scope ---------------------------------------------------------------- #
def test_in_scope_threshold_and_control_roles() -> None:
    assert SVC.in_scope(_party("a", PartyRole.BENEFICIAL_OWNER, pct=25.0)) is True
    assert SVC.in_scope(_party("b", PartyRole.BENEFICIAL_OWNER, pct=24.9)) is False
    assert SVC.in_scope(_party("c", PartyRole.DIRECTOR, pct=0.0)) is True
    assert SVC.in_scope(_party("d", PartyRole.CONTROLLER)) is True
    assert SVC.in_scope(_party("e", PartyRole.OTHER, pct=99.0)) is False


# --- roll-up -------------------------------------------------------------- #
def test_clean_director_clears() -> None:
    e = RelatedPartyInput(_party("Dir", PartyRole.DIRECTOR), PartyScreening(identity_verified=True))
    review = SVC.assess([e])
    o = review.outcomes[0]
    assert o.in_scope and o.cleared and not o.escalates
    assert review.escalated is False
    assert review.cleared_count == 1


def test_pep_escalates_even_if_otherwise_clean() -> None:
    e = RelatedPartyInput(
        _party("Pep", PartyRole.DIRECTOR),
        PartyScreening(identity_verified=True, is_pep=True),
    )
    review = SVC.assess([e])
    assert review.escalated is True
    assert "politically-exposed person" in review.outcomes[0].reasons


def test_sanctions_and_high_media_escalate() -> None:
    sanctions = RelatedPartyInput(
        _party("S", PartyRole.DIRECTOR),
        PartyScreening(identity_verified=True, sanctions_hit=True),
    )
    media = RelatedPartyInput(
        _party("M", PartyRole.DIRECTOR),
        PartyScreening(identity_verified=True, adverse_media=Severity.HIGH),
    )
    assert SVC.assess([sanctions]).escalated is True
    assert SVC.assess([media]).escalated is True


def test_source_of_funds_requires_cleared_sow() -> None:
    party = _party("UBO", PartyRole.BENEFICIAL_OWNER, pct=60.0, sof=True)
    # No SoW sub-case yet -> not cleared, escalates.
    not_started = SVC.assess([RelatedPartyInput(party, PartyScreening(identity_verified=True))])
    o = not_started.outcomes[0]
    assert not o.cleared and o.escalates
    assert any("source-of-funds SoW" in r for r in o.reasons)

    # SoW ready for review -> cleared, no escalation.
    ready = SVC.assess(
        [
            RelatedPartyInput(
                party,
                PartyScreening(identity_verified=True),
                sow_case_id="sow-ubo",
                sow_status=CaseStatus.READY_FOR_REVIEW,
                sow_coverage_pct=0.9,
            )
        ]
    )
    assert ready.outcomes[0].cleared and not ready.escalated


def test_identity_not_verified_is_not_cleared() -> None:
    e = RelatedPartyInput(_party("X", PartyRole.DIRECTOR), PartyScreening(identity_verified=False))
    o = SVC.assess([e]).outcomes[0]
    assert not o.cleared
    assert "identity not verified" in o.reasons


def test_out_of_scope_party_never_escalates_parent() -> None:
    # A 5% owner who is a PEP is out of scope -> does not escalate the parent.
    e = RelatedPartyInput(
        _party("Minor", PartyRole.BENEFICIAL_OWNER, pct=5.0),
        PartyScreening(identity_verified=False, is_pep=True),
    )
    review = SVC.assess([e])
    assert review.outcomes[0].in_scope is False
    assert review.outcomes[0].escalates is False
    assert review.escalated is False


def test_summary_counts_in_scope_only() -> None:
    entries = [
        RelatedPartyInput(
            _party("Dir", PartyRole.DIRECTOR), PartyScreening(identity_verified=True)
        ),
        RelatedPartyInput(
            _party("Minor", PartyRole.BENEFICIAL_OWNER, pct=1.0),
            PartyScreening(identity_verified=True),
        ),
    ]
    review = SVC.assess(entries)
    assert len(review.in_scope) == 1
    assert "1/1 in-scope" in review.summary


def test_policy_soft_escalation() -> None:
    pol = RelatedPartyPolicy()
    assert pol.requires_enhanced_review(None) is False
    clean = SVC.assess(
        [
            RelatedPartyInput(
                _party("Dir", PartyRole.DIRECTOR), PartyScreening(identity_verified=True)
            )
        ]
    )
    assert pol.requires_enhanced_review(clean) is False
    pep = SVC.assess(
        [
            RelatedPartyInput(
                _party("Pep", PartyRole.DIRECTOR),
                PartyScreening(identity_verified=True, is_pep=True),
            )
        ]
    )
    assert pol.requires_enhanced_review(pep) is True


def test_deterministic() -> None:
    entries = [
        RelatedPartyInput(
            _party("UBO", PartyRole.BENEFICIAL_OWNER, pct=60.0, sof=True),
            PartyScreening(identity_verified=True, is_pep=True),
        )
    ]
    a, b = SVC.assess(entries), SVC.assess(entries)
    assert a == b
