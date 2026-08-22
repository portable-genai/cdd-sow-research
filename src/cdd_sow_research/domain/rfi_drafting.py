"""RfiDraftingService — turn computed gaps into client-ready information requests.

The *what* and *why* of every RFI come from the deterministic :class:`Gap`; only the
*wording* is a presentation concern. The default drafter is template-based (pure,
testable, offline). An implementation may swap in an LLM for nicer prose without changing
the mapping — the gap-to-RFI link, priority, and suggested document types stay
deterministic so the audit trail holds.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import (
    Gap,
    GapKind,
    InformationRequest,
    RfiStatus,
    WealthSourceKind,
)

# Suggested document types per wealth-source kind (free-form; DocType is too coarse).
# Keyed by the kind's string value so deployment-specific taxonomy extensions can add
# hints without touching this module (WealthSourceKind members ARE their values).
_DOC_HINTS: dict[str, tuple[str, ...]] = {
    WealthSourceKind.EMPLOYMENT: ("employment_letter", "payslip", "tax_return"),
    WealthSourceKind.BUSINESS_OWNERSHIP: (
        "share_purchase_agreement",
        "company_registry_extract",
        "audited_accounts",
    ),
    WealthSourceKind.INHERITANCE: ("will", "grant_of_probate", "estate_account"),
    WealthSourceKind.INVESTMENTS: ("brokerage_statement", "portfolio_valuation"),
    WealthSourceKind.ASSET_SALE: ("sale_contract", "title_deed", "bank_credit_advice"),
    WealthSourceKind.OTHER: ("supporting_documentation",),
}


@dataclass(frozen=True, slots=True)
class RfiDraftingService:
    """Deterministic template drafter (signature stable; LLM is an optional swap)."""

    default_due_days: int = 21
    _doc_hints: dict[str, tuple[str, ...]] = field(default_factory=lambda: dict(_DOC_HINTS))

    def draft(self, gaps: tuple[Gap, ...]) -> tuple[InformationRequest, ...]:
        """One client-ready RFI per gap, linked back to the gap it closes."""
        return tuple(self._one(g) for g in gaps)

    def _one(self, gap: Gap) -> InformationRequest:
        return InformationRequest(
            id=f"rfi-{gap.id}",
            gap_id=gap.id,
            ask=self._ask(gap),
            suggested_doc_types=self._doc_hints.get(gap.related_kind, ())
            if gap.related_kind
            else (),
            priority=gap.severity,
            status=RfiStatus.DRAFT,
        )

    def _ask(self, gap: Gap) -> str:
        kind = str(gap.related_kind).replace("_", " ") if gap.related_kind else ""
        if gap.kind is GapKind.MISSING_CORROBORATION:
            return (
                f"Please provide documentary evidence for the declared {kind} "
                f"(e.g. {self._hint(gap)})."
            )
        if gap.kind is GapKind.MISSING_MANDATORY_DOC:
            return (
                f"Please provide the required supporting document for {kind} ({self._hint(gap)})."
            )
        if gap.kind is GapKind.STALE_EVIDENCE:
            return (
                "Please provide an up-to-date version of the document; the one on file is "
                "outside the validity window."
            )
        if gap.kind is GapKind.UNRECONCILED_DELTA:
            return (
                "Please explain and evidence the difference between your declared net worth "
                "and the documents provided so far."
            )
        if gap.kind is GapKind.INCONSISTENT_VALUE:
            return f"Please reconcile the differing values reported for your {kind}."
        if gap.kind is GapKind.UNVERIFIED_PEP_LINK:
            return "Please provide source-of-funds evidence for the politically-exposed holding."
        return "Please provide additional supporting information."

    def _hint(self, gap: Gap) -> str:
        hints = self._doc_hints.get(gap.related_kind, ()) if gap.related_kind else ()
        return ", ".join(h.replace("_", " ") for h in hints[:3]) or "supporting documentation"
