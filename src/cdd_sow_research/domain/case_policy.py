"""CaseTransitionPolicy — the SoW case state-machine guard (P-06 four-eyes).

The longitudinal sibling of :class:`~cdd_sow_research.domain.review_policy.CddReviewPolicy`.
It centralises two rules so they are auditable in one place:

* **Legal transitions only.** The state machine in ``docs/sow-longitudinal-audit-design.md``
  is encoded as an allow-list; any other move is rejected, so the audit trail can never
  show an impossible history.
* **Four-eyes approval.** Moving to ``APPROVED`` requires a checker whose identity differs
  from the maker who produced the analysis (maker-checker, P-06).
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import CaseStatus

#: from_state -> the set of states it may legally transition to.
_ALLOWED: dict[CaseStatus, frozenset[CaseStatus]] = {
    CaseStatus.DRAFT: frozenset({CaseStatus.GATHERING, CaseStatus.WITHDRAWN}),
    CaseStatus.GATHERING: frozenset(
        {CaseStatus.ANALYSING, CaseStatus.ON_HOLD, CaseStatus.BLOCKED, CaseStatus.WITHDRAWN}
    ),
    CaseStatus.ANALYSING: frozenset(
        {CaseStatus.RFI_PENDING, CaseStatus.READY_FOR_REVIEW, CaseStatus.BLOCKED}
    ),
    CaseStatus.RFI_PENDING: frozenset(
        {CaseStatus.GATHERING, CaseStatus.RFI_PENDING, CaseStatus.ON_HOLD}
    ),
    CaseStatus.READY_FOR_REVIEW: frozenset({CaseStatus.IN_REVIEW, CaseStatus.GATHERING}),
    CaseStatus.IN_REVIEW: frozenset({CaseStatus.APPROVED, CaseStatus.GATHERING}),
    CaseStatus.APPROVED: frozenset({CaseStatus.GATHERING}),  # periodic refresh re-opens
    CaseStatus.BLOCKED: frozenset({CaseStatus.GATHERING}),
    CaseStatus.ON_HOLD: frozenset({CaseStatus.GATHERING, CaseStatus.WITHDRAWN}),
    CaseStatus.WITHDRAWN: frozenset(),
}


@dataclass(frozen=True, slots=True)
class CaseTransitionPolicy:
    """Pure decision logic for the case state machine; no side effects."""

    def can(self, src: CaseStatus, dst: CaseStatus) -> bool:
        """Return True if ``src -> dst`` is a legal transition."""
        return dst in _ALLOWED.get(src, frozenset())

    def allowed_from(self, src: CaseStatus) -> frozenset[CaseStatus]:
        """The set of states reachable in one step from ``src``."""
        return _ALLOWED.get(src, frozenset())

    def can_approve(self, maker: str, checker: str) -> bool:
        """Four-eyes: a checker may approve only if they are not the maker (P-06)."""
        m, c = (maker or "").strip().lower(), (checker or "").strip().lower()
        return bool(c) and m != c
