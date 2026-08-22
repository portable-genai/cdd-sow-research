"""SourceOfFundsService — deterministic Source-of-Funds reconciliation + gaps.

Source of Funds (SoF) is distinct from Source of Wealth (SoW): SoW explains the origin of
the customer's *total accumulated wealth*; SoF explains the origin of the *specific funds*
flowing into the relationship, and whether actual inflows match the declared
expected-activity profile. This is a pure, replayable reconciliation of declared expected
funding vs evidenced inflows (e.g. bank credit advices), producing ranked gaps an analyst
dispositions. No LLM, no I/O — same inputs, same result.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from . import value_bands as vb
from .models import (
    Citation,
    DeclaredFunds,
    FundsDeclaration,
    FundsFlow,
    FundsGap,
    FundsGapKind,
    FundsLine,
    Severity,
    SourceOfFundsAssessment,
)
from .policy import SofPolicy


@dataclass(frozen=True, slots=True)
class SourceOfFundsService:
    """Pure, deterministic SoF reconciliation. Tolerance configurable.

    Typed on plain ``str`` funds-origin kinds (the ``FundsOriginKind`` StrEnum members
    ARE strings), so a deployment can extend the taxonomy without editing the engine.
    """

    #: Coverage below ``1 - delta_tolerance`` raises an unevidenced-funding gap.
    delta_tolerance: float = 0.15
    #: Evidenced inflow above expected by more than this fraction is an activity mismatch.
    activity_tolerance: float = 0.25

    @classmethod
    def from_policy(cls, policy: SofPolicy) -> SourceOfFundsService:
        """Build the engine from a bank-owned :class:`SofPolicy` (settings-driven)."""
        return cls(
            delta_tolerance=policy.delta_tolerance,
            activity_tolerance=policy.activity_tolerance,
        )

    def assess(
        self,
        subject_id: str,
        declaration: FundsDeclaration | None,
        flows: Sequence[FundsFlow],
    ) -> SourceOfFundsAssessment:
        """Reconcile declared expected funding vs evidenced inflows; compute gaps."""
        declared = {s.kind: s for s in (declaration.sources if declaration else ())}
        currency = self._currency(declaration, flows)
        kinds = self._ordered_kinds(declared, flows)

        lines: list[FundsLine] = []
        gaps: list[FundsGap] = []
        for kind in kinds:
            matched = [f for f in flows if f.kind == kind and f.corroborated]
            declared_band = declared[kind].expected_band if kind in declared else ""
            declared_rng = vb.parse_band(declared_band)
            ev_rng, ev_band = self._sum_flows(matched, currency)
            cov = vb.coverage_pct(declared_rng, ev_rng)
            citations = self._dedupe(c for f in matched for c in f.citations)
            lines.append(
                FundsLine(
                    kind=kind,
                    declared_band=declared_band,
                    evidenced_band=ev_band,
                    coverage_pct=round(cov, 4),
                    corroborated=bool(matched),
                    citations=citations,
                )
            )
            gaps.extend(self._kind_gaps(kind, kind in declared, matched, flows))

        declared_total_band, declared_total = self._declared_total(declaration, lines, currency)
        evidenced_total, evidenced_total_band = self._sum_flows(
            [f for f in flows if f.corroborated], currency
        )
        cov_total = vb.coverage_pct(declared_total, evidenced_total)

        notes: list[str] = []
        gaps.extend(self._total_gaps(declared_total, evidenced_total, declared_total_band, notes))

        return SourceOfFundsAssessment(
            subject_id=subject_id,
            declared_inflow_band=declared_total_band,
            evidenced_inflow_band=evidenced_total_band,
            coverage_pct=round(cov_total, 4),
            expected_activity=declaration.expected_activity if declaration else "",
            lines=tuple(lines),
            gaps=tuple(self._rank(gaps)),
            consistency_notes=tuple(notes),
        )

    # ------------------------------------------------------------------ #
    def _kind_gaps(
        self,
        kind: str,
        is_declared: bool,
        matched: list[FundsFlow],
        flows: Sequence[FundsFlow],
    ) -> list[FundsGap]:
        label = str(kind).replace("_", " ")
        gaps: list[FundsGap] = []
        if is_declared and not matched:
            gaps.append(
                FundsGap(
                    id=f"sof-unevidenced-{kind}",
                    kind=FundsGapKind.UNEVIDENCED_INFLOW,
                    severity=Severity.HIGH,
                    summary=f"Declared {label} funding has no evidenced inflow.",
                    related_kind=kind,
                    detail="No corroborated inflow supports this declared funding source.",
                )
            )
        # An evidenced flow whose origin was never declared = unexplained inflow.
        if not is_declared and any(f.kind == kind for f in flows):
            gaps.append(
                FundsGap(
                    id=f"sof-unexpected-{kind}",
                    kind=FundsGapKind.UNEXPECTED_INFLOW,
                    severity=Severity.HIGH,
                    summary=f"Evidenced {label} inflow has no declared origin.",
                    related_kind=kind,
                    detail="Funds received for an undeclared origin; explain and evidence.",
                )
            )
        # Declared origin with an uncorroborated flow on file = missing origin document.
        uncorroborated = [f for f in flows if f.kind == kind and not f.corroborated]
        if is_declared and uncorroborated and not matched:
            gaps.append(
                FundsGap(
                    id=f"sof-missing-doc-{kind}",
                    kind=FundsGapKind.MISSING_ORIGIN_DOC,
                    severity=Severity.MEDIUM,
                    summary=f"{label.title()} inflow lacks an origin document.",
                    related_kind=kind,
                    detail="An inflow is recorded but no source-of-funds document corroborates it.",
                )
            )
        return gaps

    def _total_gaps(
        self,
        declared_total: vb.Range | None,
        evidenced_total: vb.Range | None,
        declared_total_band: str,
        notes: list[str],
    ) -> list[FundsGap]:
        gaps: list[FundsGap] = []
        # Activity mismatch: inflows materially exceed the expected funding profile.
        if (
            declared_total
            and evidenced_total
            and evidenced_total.midpoint > declared_total.midpoint * (1 + self.activity_tolerance)
        ):
            notes.append("Evidenced inflows exceed the declared expected activity.")
            pct = round(self.activity_tolerance * 100)
            gaps.append(
                FundsGap(
                    id="sof-activity-mismatch",
                    kind=FundsGapKind.ACTIVITY_MISMATCH,
                    severity=Severity.HIGH,
                    summary="Inflows exceed the declared expected activity.",
                    detail=(
                        f"Evidenced inflow exceeds declared {declared_total_band} "
                        f"by more than {pct}%; review for unexpected activity."
                    ),
                )
            )
        return gaps

    # ------------------------------------------------------------------ #
    @staticmethod
    def _ordered_kinds(declared: dict[str, DeclaredFunds], flows: Sequence[FundsFlow]) -> list[str]:
        order: list[str] = list(declared.keys())
        for f in flows:
            if f.kind not in order:
                order.append(f.kind)
        return order

    @staticmethod
    def _currency(declaration: FundsDeclaration | None, flows: Sequence[FundsFlow]) -> str:
        if declaration:
            if c := vb.currency_of(declaration.expected_inflow_band):
                return c
            for s in declaration.sources:
                if c := vb.currency_of(s.expected_band):
                    return c
        for f in flows:
            if c := vb.currency_of(f.amount_band):
                return c
        return "USD"

    @staticmethod
    def _sum_flows(flows: Sequence[FundsFlow], currency: str) -> tuple[vb.Range | None, str]:
        return vb.sum_bands([f.amount_band for f in flows], currency)

    def _declared_total(
        self,
        declaration: FundsDeclaration | None,
        lines: list[FundsLine],
        currency: str,
    ) -> tuple[str, vb.Range | None]:
        if declaration and declaration.expected_inflow_band:
            return declaration.expected_inflow_band, vb.parse_band(declaration.expected_inflow_band)
        rng, band = vb.sum_bands([ln.declared_band for ln in lines], currency)
        return band, rng

    @staticmethod
    def _dedupe(citations) -> tuple[Citation, ...]:
        seen: set[tuple[str, int | None]] = set()
        out: list[Citation] = []
        for c in citations:
            key = (c.source_id, c.page)
            if key not in seen:
                seen.add(key)
                out.append(c)
        return tuple(out)

    @staticmethod
    def _rank(gaps: list[FundsGap]) -> list[FundsGap]:
        order = {Severity.CRITICAL: 0, Severity.HIGH: 1, Severity.MEDIUM: 2, Severity.LOW: 3}
        return sorted(gaps, key=lambda g: (order.get(g.severity, 9), g.id))
