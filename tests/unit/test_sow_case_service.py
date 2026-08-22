"""Unit tests for the long-running SoW case orchestrator + state machine."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tests.fixtures.in_memory_case_store import InMemoryCaseStore

from cdd_sow_research.domain.case_policy import CaseTransitionPolicy
from cdd_sow_research.domain.errors import (
    ConcurrencyError,
    FourEyesError,
    InvalidTransitionError,
)
from cdd_sow_research.domain.models import (
    CaseStatus,
    Citation,
    DeclaredSource,
    DocType,
    EvidenceItem,
    KycDocument,
    SourceType,
    Subject,
    SubjectType,
    WealthDeclaration,
    WealthSourceKind,
)
from cdd_sow_research.domain.sow_case_service import SowCaseService
from cdd_sow_research.ports import CaseStorePort

K = WealthSourceKind
PRIN = ("case:acme",)
RM = "rm@bank.test"
MLRO = "mlro@bank.test"


def _svc() -> tuple[SowCaseService, InMemoryCaseStore]:
    store = InMemoryCaseStore()
    return SowCaseService(store=store), store


def _subject() -> Subject:
    return Subject(id="acme", name="Acme", type=SubjectType.ENTITY, jurisdiction="SG")


def _decl() -> WealthDeclaration:
    return WealthDeclaration(
        sources=(DeclaredSource(K.BUSINESS_OWNERSHIP, "sale", "USD 25m-50m"),),
        declared_net_worth_band="USD 25m-50m",
    )


def _ev(eid: str) -> EvidenceItem:
    return EvidenceItem(
        id=eid,
        document=KycDocument(id=eid, doc_type=DocType.REGISTRY_EXTRACT),
        supports_kinds=(K.BUSINESS_OWNERSHIP,),
        evidenced_band="USD 25m-50m",
        doc_date="2026-02-01",
        idempotency_key=eid,
        citations=(Citation(source_id=eid, source_type=SourceType.DOCUMENT, title=eid, page=1),),
    )


def test_in_memory_store_satisfies_port() -> None:
    assert isinstance(InMemoryCaseStore(), CaseStorePort)


def test_open_creates_draft_at_version_zero() -> None:
    svc, _ = _svc()
    case = svc.open("acme", _subject(), _decl(), actor=RM)
    assert case.status is CaseStatus.DRAFT
    assert case.version == 0


def test_full_happy_path_to_approved_snapshot() -> None:
    svc, store = _svc()
    svc.open("acme", _subject(), _decl(), actor=RM)
    svc.add_evidence("acme", PRIN, [_ev("acra")], actor=RM)
    case = svc.analyze("acme", PRIN, actor=RM, as_of=datetime(2026, 3, 1, tzinfo=UTC))
    # Fully evidenced and corroborated -> ready for review, no gaps.
    assert case.status is CaseStatus.READY_FOR_REVIEW
    assert case.current is not None
    assert case.current.gaps == ()

    final = svc.review("acme", PRIN, approve=True, checker=MLRO)
    assert final.status is CaseStatus.APPROVED
    snap = store.get_snapshot(final.id, final.version)
    assert snap.approved_by == MLRO


def test_analyze_with_gaps_goes_to_rfi_pending() -> None:
    svc, _ = _svc()
    decl = WealthDeclaration(
        sources=(DeclaredSource(K.EMPLOYMENT, "CEO", "USD 5m-10m"),),
        declared_net_worth_band="USD 5m-10m",
    )
    svc.open("acme", _subject(), decl, actor=RM)
    svc.add_evidence("acme", PRIN, [], actor=RM)
    case = svc.analyze("acme", PRIN, actor=RM)
    assert case.status is CaseStatus.RFI_PENDING
    assert len(case.current.gaps) >= 1
    assert len(case.current.rfis) == len(case.current.gaps)


def test_idempotent_evidence_intake() -> None:
    svc, _ = _svc()
    svc.open("acme", _subject(), _decl(), actor=RM)
    svc.add_evidence("acme", PRIN, [_ev("acra")], actor=RM)
    case = svc.add_evidence("acme", PRIN, [_ev("acra")], actor=RM)  # same key again
    assert len([it for it in case.ledger if it.id == "acra"]) == 1


def test_four_eyes_blocks_self_approval() -> None:
    svc, _ = _svc()
    svc.open("acme", _subject(), _decl(), actor=RM)
    svc.add_evidence("acme", PRIN, [_ev("acra")], actor=RM)
    svc.analyze("acme", PRIN, actor=RM)
    with pytest.raises(FourEyesError):
        svc.review("acme", PRIN, approve=True, checker=RM)  # same identity as maker


def test_reject_reopens_to_gathering() -> None:
    svc, _ = _svc()
    svc.open("acme", _subject(), _decl(), actor=RM)
    svc.add_evidence("acme", PRIN, [_ev("acra")], actor=RM)
    svc.analyze("acme", PRIN, actor=RM)
    case = svc.review("acme", PRIN, approve=False, checker=MLRO)
    assert case.status is CaseStatus.GATHERING


def test_optimistic_concurrency_rejects_stale_write() -> None:
    svc, store = _svc()
    svc.open("acme", _subject(), _decl(), actor=RM)
    stale = store.load("acme", PRIN)  # version 0 snapshot held by a slow actor
    svc.add_evidence("acme", PRIN, [_ev("acra")], actor=RM)  # bumps to v1
    with pytest.raises(ConcurrencyError):
        store.save(stale, expected_version=0)


def test_append_only_iterations_accumulate() -> None:
    svc, _ = _svc()
    svc.open("acme", _subject(), _decl(), actor=RM)
    svc.add_evidence("acme", PRIN, [_ev("a")], actor=RM)
    svc.analyze("acme", PRIN, actor=RM)
    svc.add_evidence("acme", PRIN, [_ev("b")], actor=RM)
    case = svc.analyze("acme", PRIN, actor=RM)
    assert len(case.iterations) == 2
    assert [i.no for i in case.iterations] == [0, 1]


# --- state machine policy ------------------------------------------------- #
def test_policy_legal_and_illegal_transitions() -> None:
    pol = CaseTransitionPolicy()
    assert pol.can(CaseStatus.DRAFT, CaseStatus.GATHERING)
    assert pol.can(CaseStatus.APPROVED, CaseStatus.GATHERING)  # periodic refresh
    assert not pol.can(CaseStatus.DRAFT, CaseStatus.APPROVED)
    assert not pol.can(CaseStatus.WITHDRAWN, CaseStatus.GATHERING)


def test_policy_four_eyes() -> None:
    pol = CaseTransitionPolicy()
    assert pol.can_approve("rm@bank", "mlro@bank")
    assert not pol.can_approve("rm@bank", "rm@bank")
    assert not pol.can_approve("rm@bank", "")


def test_analyze_requires_gathering_state() -> None:
    svc, _ = _svc()
    svc.open("acme", _subject(), _decl(), actor=RM)
    # No add_evidence -> still DRAFT; analyze must reject the illegal transition.
    with pytest.raises(InvalidTransitionError):
        svc.analyze("acme", PRIN, actor=RM)
