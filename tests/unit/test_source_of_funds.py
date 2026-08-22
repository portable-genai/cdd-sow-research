"""Unit tests for the deterministic Source-of-Funds reconciliation."""

from __future__ import annotations

from cdd_sow_research.domain.models import (
    Citation,
    DeclaredFunds,
    FundsDeclaration,
    FundsFlow,
    FundsGapKind,
    FundsOriginKind,
    SourceType,
)
from cdd_sow_research.domain.source_of_funds_service import SourceOfFundsService

F = FundsOriginKind
SVC = SourceOfFundsService()
_CITE = (
    Citation(source_id="credit", source_type=SourceType.DOCUMENT, title="Bank credit", page=1),
)


def _flow(fid, kind, band, cited=True, value_date="2026-02-01"):
    return FundsFlow(
        id=fid,
        kind=kind,
        description=fid,
        amount_band=band,
        value_date=value_date,
        citations=_CITE if cited else (),
    )


def _decl(sources, expected=""):
    return FundsDeclaration(sources=tuple(sources), expected_inflow_band=expected)


def test_fully_evidenced_no_gaps() -> None:
    decl = _decl([DeclaredFunds(F.ASSET_SALE, "sale", "USD 25m-50m")], expected="USD 25m-50m")
    a = SVC.assess("s", decl, [_flow("f1", F.ASSET_SALE, "USD 25m-50m")])
    assert a.gaps == ()
    assert a.escalates is False
    assert a.coverage_pct == 1.0
    assert a.declared_inflow_band == "USD 25m-50m"


def test_unevidenced_declared_funding() -> None:
    decl = _decl([DeclaredFunds(F.SALARY, "salary", "USD 1m-2m")], expected="USD 1m-2m")
    a = SVC.assess("s", decl, [])
    assert any(g.kind is FundsGapKind.UNEVIDENCED_INFLOW for g in a.gaps)
    assert a.escalates is True


def test_unexpected_inflow_not_declared() -> None:
    decl = _decl([DeclaredFunds(F.SALARY, "salary", "USD 1m-2m")], expected="USD 1m-2m")
    flows = [_flow("f1", F.SALARY, "USD 1m-2m"), _flow("f2", F.GIFT, "USD 5m-10m")]
    a = SVC.assess("s", decl, flows)
    g = next(x for x in a.gaps if x.kind is FundsGapKind.UNEXPECTED_INFLOW)
    assert g.related_kind is F.GIFT


def test_missing_origin_doc_when_flow_uncorroborated() -> None:
    decl = _decl([DeclaredFunds(F.LOAN, "loan", "USD 2m-3m")], expected="USD 2m-3m")
    a = SVC.assess("s", decl, [_flow("f1", F.LOAN, "USD 2m-3m", cited=False)])
    kinds = {g.kind for g in a.gaps}
    assert FundsGapKind.MISSING_ORIGIN_DOC in kinds


def test_activity_mismatch_when_inflows_exceed_expected() -> None:
    decl = _decl([DeclaredFunds(F.SALARY, "salary", "USD 1m-1m")], expected="USD 1m-1m")
    a = SVC.assess("s", decl, [_flow("f1", F.SALARY, "USD 5m-5m")])
    assert any(g.kind is FundsGapKind.ACTIVITY_MISMATCH for g in a.gaps)
    assert any("exceed" in n for n in a.consistency_notes)


def test_coverage_and_totals() -> None:
    decl = _decl(
        [
            DeclaredFunds(F.ASSET_SALE, "sale", "USD 20m-20m"),
            DeclaredFunds(F.SALARY, "salary", "USD 0-0"),
        ],
        expected="USD 20m-20m",
    )
    a = SVC.assess("s", decl, [_flow("f1", F.ASSET_SALE, "USD 10m-10m")])
    assert 0.0 <= a.coverage_pct <= 1.0
    assert a.evidenced_inflow_band  # non-empty


def test_gaps_ranked_high_before_medium() -> None:
    decl = _decl(
        [
            DeclaredFunds(F.SALARY, "salary", "USD 1m-2m"),
            DeclaredFunds(F.LOAN, "loan", "USD 2m-3m"),
        ],
        expected="USD 3m-5m",
    )
    flows = [_flow("f1", F.LOAN, "USD 2m-3m", cited=False)]  # loan uncorroborated, salary missing
    a = SVC.assess("s", decl, flows)
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    sev = [order[g.severity.value] for g in a.gaps]
    assert sev == sorted(sev)


def test_deterministic() -> None:
    decl = _decl([DeclaredFunds(F.ASSET_SALE, "sale", "USD 25m-50m")], expected="USD 25m-50m")
    flows = [_flow("f1", F.ASSET_SALE, "USD 25m-50m"), _flow("f2", F.GIFT, "USD 5m-10m")]
    a = SVC.assess("s", decl, flows)
    b = SVC.assess("s", decl, flows)
    assert [g.id for g in a.gaps] == [g.id for g in b.gaps]
    assert a.coverage_pct == b.coverage_pct
