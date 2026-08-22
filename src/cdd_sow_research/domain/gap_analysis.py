"""GapAnalysisService — the deterministic reconciliation + gap engine.

This is the auditable core of a SoW case. It is a **pure function** of the case's
declaration and evidence ledger: the same inputs always yield the same source groups,
the same reconciliation numbers, and the same ranked gaps. No LLM, no I/O, no clock
except an explicit ``as_of`` argument. The LLM layer only *narrates* and *drafts RFI
wording* on top of what this engine computes (see ``docs/sow-longitudinal-audit-design.md``).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime

from . import value_bands as vb
from .models import (
    Citation,
    DeclaredSource,
    EvidenceItem,
    Gap,
    GapKind,
    ReconciliationLine,
    Severity,
    SourceGroup,
    SowCase,
    WealthReconciliation,
    utcnow,
)
from .policy import _DEFAULT_MANDATORY_DOCS, _DEFAULT_STALE_DOCS, GapPolicy

#: A read-only view of the declared sources keyed by wealth-source kind. The engine is
#: typed on plain ``str`` kinds (the ``WealthSourceKind`` StrEnum members ARE strings),
#: so a deployment can extend the taxonomy without editing this module.
DeclaredMap = Mapping[str, DeclaredSource]


@dataclass(frozen=True, slots=True)
class GapAnalysisResult:
    """The engine's output: grouped sources, reconciliation, and ranked gaps."""

    groups: tuple[SourceGroup, ...]
    reconciliation: WealthReconciliation
    gaps: tuple[Gap, ...]


@dataclass(frozen=True, slots=True)
class GapAnalysisService:
    """Pure, deterministic reconciliation + gap detection. Signature is stable."""

    #: A total coverage below ``1 - delta_tolerance`` raises an UNRECONCILED_DELTA gap.
    delta_tolerance: float = 0.15
    #: Documents in :data:`stale_doc_types` older than this many days are STALE_EVIDENCE.
    stale_days: int = 180
    mandatory_docs: Mapping[str, frozenset[str]] = field(
        default_factory=lambda: dict(_DEFAULT_MANDATORY_DOCS)
    )
    stale_doc_types: frozenset[str] = _DEFAULT_STALE_DOCS

    @classmethod
    def from_policy(cls, policy: GapPolicy) -> GapAnalysisService:
        """Build the engine from a bank-owned :class:`GapPolicy` (settings-driven)."""
        return cls(
            delta_tolerance=policy.delta_tolerance,
            stale_days=policy.stale_days,
            mandatory_docs=dict(policy.mandatory_docs),
            stale_doc_types=policy.stale_doc_types,
        )

    # ------------------------------------------------------------------ #
    def analyze(self, case: SowCase, as_of: datetime | None = None) -> GapAnalysisResult:
        """Compute groups, reconciliation and gaps for ``case`` (deterministic)."""
        as_of = as_of or utcnow()
        declared = {s.kind: s for s in (case.declaration.sources if case.declaration else ())}
        kinds = self._ordered_kinds(case, declared)
        currency = self._currency(case)

        groups: list[SourceGroup] = []
        lines: list[ReconciliationLine] = []
        gaps: list[Gap] = []

        for kind in kinds:
            items = [it for it in case.ledger if kind in it.supports_kinds]
            corroborated = [it for it in items if it.corroborated]
            declared_band = declared[kind].declared_band if kind in declared else ""
            declared_rng = vb.parse_band(declared_band)
            # Per kind, documents *corroborate the same tranche*; take a representative
            # range (not a sum, which would double-count multiple proofs of one source).
            evidenced_rng, evidenced_band = self._representative(corroborated, currency)
            cov = vb.coverage_pct(declared_rng, evidenced_rng)
            citations = self._dedupe(c for it in corroborated for c in it.citations)

            groups.append(
                SourceGroup(
                    kind=kind,
                    declared_band=declared_band,
                    evidenced_band=evidenced_band,
                    corroborated=bool(corroborated),
                    evidence_ids=tuple(it.id for it in items),
                    citations=citations,
                )
            )
            lines.append(
                ReconciliationLine(
                    kind=kind,
                    declared_band=declared_band,
                    evidenced_band=evidenced_band,
                    delta_note=self._delta_note(declared_rng, evidenced_rng, cov, currency),
                    coverage_pct=round(cov, 4),
                    corroborated=bool(corroborated),
                    citations=citations,
                )
            )
            gaps.extend(self._source_gaps(kind, declared, items, corroborated, as_of))

        reconciliation = self._reconcile(case, declared, lines, currency)
        gaps.extend(self._total_gaps(reconciliation))
        gaps = self._rank(gaps)
        return GapAnalysisResult(tuple(groups), reconciliation, tuple(gaps))

    # ------------------------------------------------------------------ #
    # Reconciliation
    # ------------------------------------------------------------------ #
    def _reconcile(
        self,
        case: SowCase,
        declared: DeclaredMap,
        lines: list[ReconciliationLine],
        currency: str,
    ) -> WealthReconciliation:
        declared_total_band = ""
        if case.declaration and case.declaration.declared_net_worth_band:
            declared_total_band = case.declaration.declared_net_worth_band
            declared_total = vb.parse_band(declared_total_band)
        else:
            declared_total, declared_total_band = vb.sum_bands(
                [ln.declared_band for ln in lines], currency
            )
        evidenced_total, evidenced_total_band = vb.sum_bands(
            [ln.evidenced_band for ln in lines], currency
        )
        cov = vb.coverage_pct(declared_total, evidenced_total)

        notes: list[str] = []
        uncorroborated = [str(ln.kind) for ln in lines if not ln.corroborated]
        if uncorroborated:
            notes.append("Uncorroborated sources: " + ", ".join(uncorroborated) + ".")
        if declared_total and evidenced_total:
            shortfall = vb.Range(
                max(0.0, declared_total.low - evidenced_total.high),
                max(0.0, declared_total.high - evidenced_total.low),
            )
            if shortfall.high > 0:
                notes.append(
                    "Unevidenced headroom up to "
                    + vb.format_range(shortfall, currency)
                    + " against the declared net worth."
                )
        return WealthReconciliation(
            lines=tuple(lines),
            declared_total_band=declared_total_band,
            evidenced_total_band=evidenced_total_band,
            coverage_pct=round(cov, 4),
            consistency_notes=tuple(notes),
        )

    # ------------------------------------------------------------------ #
    # Gap detectors (each deterministic)
    # ------------------------------------------------------------------ #
    def _source_gaps(
        self,
        kind: str,
        declared: DeclaredMap,
        items: list[EvidenceItem],
        corroborated: list[EvidenceItem],
        as_of: datetime,
    ) -> list[Gap]:
        gaps: list[Gap] = []
        is_declared = kind in declared
        label = str(kind).replace("_", " ")

        # 1. A declared source with no corroborating evidence at all.
        if is_declared and not corroborated:
            gaps.append(
                Gap(
                    id=f"gap-missing-{kind}",
                    kind=GapKind.MISSING_CORROBORATION,
                    severity=Severity.HIGH,
                    summary=f"Declared {label} has no corroborating evidence.",
                    related_kind=kind,
                    evidence_ids=tuple(it.id for it in items),
                    detail="No submitted document with a citation supports this declared source.",
                )
            )

        # 2. A policy-mandatory document type is missing for this kind.
        required = self.mandatory_docs.get(kind, frozenset())
        present = {str(it.document.doc_type) for it in items}
        missing = required - present
        if is_declared and corroborated and missing:
            names = ", ".join(sorted(str(d) for d in missing))
            gaps.append(
                Gap(
                    id=f"gap-mandatory-{kind}",
                    kind=GapKind.MISSING_MANDATORY_DOC,
                    severity=Severity.MEDIUM,
                    summary=f"{label.title()} is missing a required document ({names}).",
                    related_kind=kind,
                    evidence_ids=tuple(it.id for it in items),
                    detail=f"Policy requires {names} for this source kind; none is on file.",
                )
            )

        # 3. Stale evidence — group level: a decaying source is stale only when *all* its
        #    decaying documents are out of window (a fresh re-submission clears it).
        stale_candidates = [it for it in items if it.document.doc_type in self.stale_doc_types]
        if stale_candidates and all(self._is_stale(it, as_of) for it in stale_candidates):
            newest = max(stale_candidates, key=lambda it: it.doc_date or "")
            gaps.append(
                Gap(
                    id=f"gap-stale-{kind}",
                    kind=GapKind.STALE_EVIDENCE,
                    severity=Severity.MEDIUM,
                    summary=f"Evidence for {label} is older than {self.stale_days} days.",
                    related_kind=kind,
                    evidence_ids=tuple(it.id for it in stale_candidates),
                    detail=(
                        f"Most recent supporting document is dated {newest.doc_date}; a refresh "
                        f"within the {self.stale_days}-day window is required."
                    ),
                )
            )

        # 4. Corroborated items that disagree on value beyond overlap.
        if self._inconsistent(corroborated):
            gaps.append(
                Gap(
                    id=f"gap-inconsistent-{kind}",
                    kind=GapKind.INCONSISTENT_VALUE,
                    severity=Severity.HIGH,
                    summary=f"Evidence for {label} reports inconsistent values.",
                    related_kind=kind,
                    evidence_ids=tuple(it.id for it in corroborated),
                    detail="Two or more corroborating documents give non-overlapping value bands.",
                )
            )
        return gaps

    def _total_gaps(self, reconciliation: WealthReconciliation) -> list[Gap]:
        if not reconciliation.declared_total_band:
            return []
        if reconciliation.coverage_pct >= 1.0 - self.delta_tolerance:
            return []
        sev = Severity.HIGH if reconciliation.coverage_pct < 0.5 else Severity.MEDIUM
        pct = round(reconciliation.coverage_pct * 100)
        return [
            Gap(
                id="gap-unreconciled-total",
                kind=GapKind.UNRECONCILED_DELTA,
                severity=sev,
                summary=f"Declared net worth is only {pct}% evidenced.",
                related_kind=None,
                evidence_ids=(),
                detail=(
                    f"Evidenced {reconciliation.evidenced_total_band or 'nothing'} against "
                    f"declared {reconciliation.declared_total_band} "
                    f"(coverage {pct}%, tolerance {round((1 - self.delta_tolerance) * 100)}%)."
                ),
            )
        ]

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _ordered_kinds(case: SowCase, declared: DeclaredMap) -> list[str]:
        """Declared kinds first (declaration order), then any evidence-only kinds."""
        order: list[str] = list(declared.keys())
        for it in case.ledger:
            for k in it.supports_kinds:
                if k not in order:
                    order.append(k)
        return order

    @staticmethod
    def _currency(case: SowCase) -> str:
        if case.declaration:
            if c := vb.currency_of(case.declaration.declared_net_worth_band):
                return c
            for s in case.declaration.sources:
                if c := vb.currency_of(s.declared_band):
                    return c
        for it in case.ledger:
            if c := vb.currency_of(it.evidenced_band):
                return c
        return "USD"

    def _is_stale(self, item: EvidenceItem, as_of: datetime) -> bool:
        if not item.doc_date:
            return False
        try:
            dated = datetime.fromisoformat(item.doc_date)
        except ValueError:
            return False
        ref = as_of.replace(tzinfo=None) if dated.tzinfo is None else as_of
        return (ref - dated).days > self.stale_days

    @staticmethod
    def _representative(
        corroborated: list[EvidenceItem], currency: str
    ) -> tuple[vb.Range | None, str]:
        """A single representative range across documents corroborating one source.

        Documents attest the *same* tranche, so the evidenced value is their span
        (min low, max high) — never their sum. Disagreement widens the span and is
        separately reported as an INCONSISTENT_VALUE gap.
        """
        ranges = [r for r in (vb.parse_band(it.evidenced_band) for it in corroborated) if r]
        if not ranges:
            return None, ""
        rng = vb.Range(min(r.low for r in ranges), max(r.high for r in ranges))
        return rng, vb.format_range(rng, currency)

    @staticmethod
    def _inconsistent(corroborated: list[EvidenceItem]) -> bool:
        ranges = [r for r in (vb.parse_band(it.evidenced_band) for it in corroborated) if r]
        if len(ranges) < 2:
            return False
        lo = max(r.low for r in ranges)
        hi = min(r.high for r in ranges)
        return lo > hi * 1.5  # clearly disjoint, not merely a fuzzy band overlap

    @staticmethod
    def _delta_note(
        declared: vb.Range | None,
        evidenced: vb.Range | None,
        cov: float,
        currency: str,
    ) -> str:
        if declared is None:
            return "No declared figure to reconcile against."
        if evidenced is None:
            return "Declared but unevidenced."
        return f"{round(cov * 100)}% of the declared band is evidenced."

    @staticmethod
    def _dedupe(citations: Iterable[Citation]) -> tuple[Citation, ...]:
        seen: set[tuple[str, int | None]] = set()
        out: list[Citation] = []
        for c in citations:
            key = (c.source_id, c.page)
            if key not in seen:
                seen.add(key)
                out.append(c)
        return tuple(out)

    @staticmethod
    def _rank(gaps: list[Gap]) -> list[Gap]:
        order = {Severity.CRITICAL: 0, Severity.HIGH: 1, Severity.MEDIUM: 2, Severity.LOW: 3}
        return sorted(gaps, key=lambda g: (order.get(g.severity, 9), g.id))
