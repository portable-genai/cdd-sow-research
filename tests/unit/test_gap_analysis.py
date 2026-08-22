"""Unit tests for the deterministic reconciliation + gap engine."""

from __future__ import annotations

from datetime import UTC, datetime

from cdd_sow_research.domain.gap_analysis import GapAnalysisService
from cdd_sow_research.domain.models import (
    Citation,
    DeclaredSource,
    DocType,
    EvidenceItem,
    GapKind,
    KycDocument,
    Severity,
    SourceType,
    Subject,
    SubjectType,
    WealthDeclaration,
    WealthSourceKind,
)

K = WealthSourceKind
AS_OF = datetime(2026, 3, 1, tzinfo=UTC)


def _subject() -> Subject:
    return Subject(id="acme", name="Acme", type=SubjectType.ENTITY, jurisdiction="SG")


def _ev(eid, doc_type, kind, band, doc_date="2026-02-01", cited=True) -> EvidenceItem:
    cites = (
        (Citation(source_id=eid, source_type=SourceType.DOCUMENT, title=eid, page=1),)
        if cited
        else ()
    )
    return EvidenceItem(
        id=eid,
        document=KycDocument(id=eid, doc_type=doc_type),
        supports_kinds=(kind,),
        evidenced_band=band,
        doc_date=doc_date,
        citations=cites,
    )


def _case(sources, net_worth, ledger):
    from cdd_sow_research.domain.models import SowCase

    return SowCase(
        id="acme",
        subject=_subject(),
        declaration=WealthDeclaration(sources=tuple(sources), declared_net_worth_band=net_worth),
        ledger=tuple(ledger),
    )


def test_missing_corroboration_for_undocumented_source() -> None:
    case = _case(
        [DeclaredSource(K.EMPLOYMENT, "CEO", "USD 5m-10m")],
        "USD 5m-10m",
        [],
    )
    result = GapAnalysisService().analyze(case, as_of=AS_OF)
    kinds = {g.kind for g in result.gaps}
    assert GapKind.MISSING_CORROBORATION in kinds
    gap = next(g for g in result.gaps if g.kind is GapKind.MISSING_CORROBORATION)
    assert gap.severity is Severity.HIGH
    assert gap.related_kind is K.EMPLOYMENT


def test_uncited_evidence_does_not_corroborate() -> None:
    case = _case(
        [DeclaredSource(K.EMPLOYMENT, "CEO", "USD 5m-10m")],
        "USD 5m-10m",
        [_ev("e1", DocType.FIN_STATEMENT, K.EMPLOYMENT, "USD 5m-10m", cited=False)],
    )
    result = GapAnalysisService().analyze(case, as_of=AS_OF)
    assert any(g.kind is GapKind.MISSING_CORROBORATION for g in result.gaps)


def test_representative_value_not_summed_across_duplicate_proofs() -> None:
    # Two documents corroborating the SAME sale must not double the evidenced value.
    case = _case(
        [DeclaredSource(K.BUSINESS_OWNERSHIP, "sale", "USD 25m-50m")],
        "USD 25m-50m",
        [
            _ev("spa", DocType.OTHER, K.BUSINESS_OWNERSHIP, "USD 25m-50m"),
            _ev("acra", DocType.REGISTRY_EXTRACT, K.BUSINESS_OWNERSHIP, "USD 25m-50m"),
        ],
    )
    result = GapAnalysisService().analyze(case, as_of=AS_OF)
    line = result.reconciliation.lines[0]
    assert line.evidenced_band == "USD 25m-50m"  # not 50m-100m
    assert result.reconciliation.coverage_pct == 1.0


def test_missing_mandatory_doc_flagged_then_cleared() -> None:
    # Business ownership requires a registry extract.
    without = _case(
        [DeclaredSource(K.BUSINESS_OWNERSHIP, "sale", "USD 25m-50m")],
        "USD 25m-50m",
        [_ev("spa", DocType.OTHER, K.BUSINESS_OWNERSHIP, "USD 25m-50m")],
    )
    res1 = GapAnalysisService().analyze(without, as_of=AS_OF)
    assert any(g.kind is GapKind.MISSING_MANDATORY_DOC for g in res1.gaps)

    with_extract = _case(
        [DeclaredSource(K.BUSINESS_OWNERSHIP, "sale", "USD 25m-50m")],
        "USD 25m-50m",
        [
            _ev("spa", DocType.OTHER, K.BUSINESS_OWNERSHIP, "USD 25m-50m"),
            _ev("acra", DocType.REGISTRY_EXTRACT, K.BUSINESS_OWNERSHIP, "USD 25m-50m"),
        ],
    )
    res2 = GapAnalysisService().analyze(with_extract, as_of=AS_OF)
    assert not any(g.kind is GapKind.MISSING_MANDATORY_DOC for g in res2.gaps)


def test_stale_evidence_cleared_by_fresh_resubmission() -> None:
    stale_only = _case(
        [DeclaredSource(K.INVESTMENTS, "portfolio", "USD 10m-25m")],
        "USD 10m-25m",
        [_ev("old", DocType.FIN_STATEMENT, K.INVESTMENTS, "USD 10m-25m", doc_date="2023-01-01")],
    )
    res1 = GapAnalysisService().analyze(stale_only, as_of=AS_OF)
    assert any(g.kind is GapKind.STALE_EVIDENCE for g in res1.gaps)

    refreshed = _case(
        [DeclaredSource(K.INVESTMENTS, "portfolio", "USD 10m-25m")],
        "USD 10m-25m",
        [
            _ev("old", DocType.FIN_STATEMENT, K.INVESTMENTS, "USD 10m-25m", doc_date="2023-01-01"),
            _ev("new", DocType.FIN_STATEMENT, K.INVESTMENTS, "USD 10m-25m", doc_date="2026-02-20"),
        ],
    )
    res2 = GapAnalysisService().analyze(refreshed, as_of=AS_OF)
    assert not any(g.kind is GapKind.STALE_EVIDENCE for g in res2.gaps)


def test_unreconciled_delta_when_coverage_below_tolerance() -> None:
    case = _case(
        [DeclaredSource(K.EMPLOYMENT, "CEO", "USD 5m-10m")],
        "USD 100m-100m",
        [_ev("e1", DocType.FIN_STATEMENT, K.EMPLOYMENT, "USD 5m-10m")],
    )
    result = GapAnalysisService().analyze(case, as_of=AS_OF)
    assert any(g.kind is GapKind.UNRECONCILED_DELTA for g in result.gaps)


def test_no_delta_when_within_tolerance() -> None:
    case = _case(
        [DeclaredSource(K.EMPLOYMENT, "CEO", "USD 5m-10m")],
        "USD 5m-10m",
        [_ev("e1", DocType.FIN_STATEMENT, K.EMPLOYMENT, "USD 5m-10m")],
    )
    result = GapAnalysisService().analyze(case, as_of=AS_OF)
    assert not any(g.kind is GapKind.UNRECONCILED_DELTA for g in result.gaps)
    assert result.reconciliation.coverage_pct == 1.0


def test_inconsistent_value_detected() -> None:
    case = _case(
        [DeclaredSource(K.INVESTMENTS, "portfolio", "USD 10m-100m")],
        "USD 10m-100m",
        [
            _ev("a", DocType.FIN_STATEMENT, K.INVESTMENTS, "USD 5m-8m"),
            _ev("b", DocType.FIN_STATEMENT, K.INVESTMENTS, "USD 80m-100m"),
        ],
    )
    result = GapAnalysisService().analyze(case, as_of=AS_OF)
    assert any(g.kind is GapKind.INCONSISTENT_VALUE for g in result.gaps)


def test_deterministic_same_input_same_output() -> None:
    case = _case(
        [DeclaredSource(K.EMPLOYMENT, "CEO", "USD 5m-10m")],
        "USD 100m",
        [],
    )
    svc = GapAnalysisService()
    a = svc.analyze(case, as_of=AS_OF)
    b = svc.analyze(case, as_of=AS_OF)
    assert [g.id for g in a.gaps] == [g.id for g in b.gaps]
    assert a.reconciliation.coverage_pct == b.reconciliation.coverage_pct


def test_gaps_ranked_by_severity() -> None:
    case = _case(
        [
            DeclaredSource(K.EMPLOYMENT, "CEO", "USD 5m-10m"),  # missing -> HIGH
            DeclaredSource(K.INVESTMENTS, "pf", "USD 10m-25m"),
        ],
        "USD 100m",
        [_ev("old", DocType.FIN_STATEMENT, K.INVESTMENTS, "USD 10m-25m", doc_date="2023-01-01")],
    )
    result = GapAnalysisService().analyze(case, as_of=AS_OF)
    order = {Severity.CRITICAL: 0, Severity.HIGH: 1, Severity.MEDIUM: 2, Severity.LOW: 3}
    sevs = [order[g.severity] for g in result.gaps]
    assert sevs == sorted(sevs)
