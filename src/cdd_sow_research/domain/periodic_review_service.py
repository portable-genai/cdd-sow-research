"""PeriodicReviewService — deterministic ongoing monitoring + periodic review.

CDD does not stop at approval: an approved relationship must be kept current. Two forces
drive a re-review and this service computes both, with no LLM and no I/O:

* a **risk-based schedule** — EDD is reviewed far more often than SDD — yielding the next
  periodic-review date and whether the case is current / due-soon / overdue; and
* **event triggers** — a new/open sanctions hit, PEP exposure, adverse media, an ownership
  change, or unusual activity (an SoF gap) — that force an *out-of-cycle* re-review.

The outcome is a ``MonitoringAssessment``. A triggered or overdue case escalates softly to
enhanced review; a checker still disposes (P-06) — monitoring never auto-blocks. Same
inputs (case signals + ``as_of``), same result.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

from .models import (
    CddTier,
    MonitoringAssessment,
    ReviewStatus,
    ReviewTrigger,
    ReviewTriggerKind,
    RiskScorecard,
    ScreeningResult,
    Severity,
    SourceOfFundsAssessment,
)
from .policy import MonitoringPolicy

# Risk-based review cadence in months (the higher the risk, the more often). The
# canonical defaults live in policy.MonitoringPolicy (string-keyed for configuration).
_DEFAULT_CADENCE: dict[CddTier, int] = {
    CddTier(tier): months for tier, months in MonitoringPolicy().cadence_months.items()
}

_TRIGGER_ORDER = {Severity.CRITICAL: 0, Severity.HIGH: 1, Severity.MEDIUM: 2, Severity.LOW: 3}


@dataclass(frozen=True, slots=True)
class PeriodicReviewService:
    """Pure, deterministic ongoing-monitoring scheduling + trigger detection."""

    #: Cadence (months) per CDD tier; the schedule is risk-based.
    cadence_months: dict[CddTier, int] = field(default_factory=lambda: dict(_DEFAULT_CADENCE))
    #: A periodic review within this many days counts as "due soon".
    due_soon_days: int = 90

    @classmethod
    def from_policy(cls, policy: MonitoringPolicy) -> PeriodicReviewService:
        """Build the service from a bank-owned :class:`MonitoringPolicy`."""
        return cls(
            cadence_months={
                CddTier(tier): int(months) for tier, months in policy.cadence_months.items()
            },
            due_soon_days=policy.due_soon_days,
        )

    def assess(
        self,
        subject_id: str,
        scorecard: RiskScorecard | None,
        last_reviewed: str,
        as_of: date,
        triggers: Sequence[ReviewTrigger] = (),
    ) -> MonitoringAssessment:
        """Compute the next review date, the review status, and the ranked triggers."""
        tier = scorecard.tier if scorecard else CddTier.CDD
        cadence = self.cadence_months.get(tier, 36)
        last = _parse_date(last_reviewed)
        due = _add_months(last, cadence) if last else as_of
        days_until = (due - as_of).days

        ranked = self._rank(triggers)
        notes: list[str] = []
        never_reviewed = last is None

        # A never-reviewed relationship is review-due now; an elapsed schedule is overdue.
        # Both are surfaced as a synthetic PERIODIC_DUE trigger (so the case escalates) and
        # mapped to OVERDUE — unless the caller already supplied a PERIODIC_DUE trigger.
        if never_reviewed or days_until < 0:
            if not any(t.kind is ReviewTriggerKind.PERIODIC_DUE for t in ranked):
                summary = (
                    "No periodic review on record."
                    if never_reviewed
                    else "Periodic review is overdue."
                )
                detail = (
                    f"No prior periodic review recorded; a {tier.value.upper()} review is due now."
                    if never_reviewed
                    else (
                        f"{tier.value.upper()} cadence is {cadence} months; "
                        f"review was due {due.isoformat()}."
                    )
                )
                ranked = self._rank(
                    [
                        ReviewTrigger(
                            kind=ReviewTriggerKind.PERIODIC_DUE,
                            severity=Severity.HIGH,
                            summary=summary,
                            detail=detail,
                        ),
                        *triggers,
                    ]
                )
            status = ReviewStatus.OVERDUE
        elif triggers:
            status = ReviewStatus.TRIGGERED
        elif days_until <= self.due_soon_days:
            status = ReviewStatus.DUE_SOON
        else:
            status = ReviewStatus.CURRENT

        # Always record the schedule context, even when an event trigger co-occurs.
        if never_reviewed:
            notes.append("No prior periodic review on record.")
        elif days_until < 0:
            notes.append(f"Periodic review overdue by {abs(days_until)} day(s).")
        else:
            notes.append(f"Periodic review due in {days_until} day(s).")

        return MonitoringAssessment(
            subject_id=subject_id,
            tier=tier,
            last_reviewed=last.isoformat() if last else "",
            next_review_due=due.isoformat(),
            review_status=status,
            days_until_due=days_until,
            cadence_months=cadence,
            triggers=tuple(ranked),
            notes=tuple(notes),
        )

    def triggers_from_signals(
        self,
        *,
        screening: ScreeningResult | None = None,
        source_of_funds: SourceOfFundsAssessment | None = None,
        is_pep: bool = False,
    ) -> list[ReviewTrigger]:
        """Derive event triggers from a case's attached results (deterministic)."""
        out: list[ReviewTrigger] = []
        if screening and screening.open_alerts:
            out.append(
                ReviewTrigger(
                    kind=ReviewTriggerKind.SANCTIONS_HIT,
                    severity=Severity.HIGH,
                    summary=f"{len(screening.open_alerts)} open screening hit(s).",
                    detail="An open sanctions / watchlist match requires re-review.",
                )
            )
        if is_pep:
            out.append(
                ReviewTrigger(
                    kind=ReviewTriggerKind.PEP_STATUS,
                    severity=Severity.MEDIUM,
                    summary="Politically-exposed person.",
                    detail="PEP exposure keeps the relationship under enhanced monitoring.",
                )
            )
        if source_of_funds and source_of_funds.escalates:
            out.append(
                ReviewTrigger(
                    kind=ReviewTriggerKind.UNUSUAL_ACTIVITY,
                    severity=Severity.HIGH,
                    summary="Source-of-Funds gaps detected.",
                    detail="Inflows diverge from the declared funding profile; re-review.",
                )
            )
        return self._rank(out)

    @staticmethod
    def _rank(triggers: Sequence[ReviewTrigger]) -> list[ReviewTrigger]:
        return sorted(triggers, key=lambda t: (_TRIGGER_ORDER.get(t.severity, 9), t.kind.value))


def _parse_date(value: str) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value[:10])


def _add_months(d: date, months: int) -> date:
    """Add ``months`` to a date, clamping the day to the target month's length."""
    total = d.month - 1 + months
    year = d.year + total // 12
    month = total % 12 + 1
    # Clamp day (e.g. Jan 31 + 1 month -> Feb 28/29).
    day = min(d.day, _days_in_month(year, month))
    return date(year, month, day)


def _days_in_month(year: int, month: int) -> int:
    nxt = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return (nxt - date(year, month, 1)).days
