"""RelatedPartyService — derive and roll up a company's key individuals.

Onboarding a company also requires CDD/SoW on its key individuals (UBOs, directors,
controllers). This pure, deterministic service:

* **derives** related parties from the corporate `OwnershipSummary` (beneficial owners),
* decides which are **in scope** (UBOs at/above the 25% threshold, plus directors and
  controllers — see ``docs/sow-longitudinal-audit-design.md``), and
* **rolls up** each party's CDD screening (identity + PEP + adverse media) and, for
  source-of-funds individuals, their linked SoW sub-case status into a parent-level
  :class:`RelatedPartyReview`.

The roll-up is a **soft** signal: it escalates the parent to enhanced review but never
auto-blocks — a checker disposes (maker-checker, P-06). No LLM, no I/O; the same inputs
always yield the same review, so an auditor can recompute it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .models import (
    CaseStatus,
    OwnershipSummary,
    PartyRole,
    PartyScreening,
    RelatedParty,
    RelatedPartyOutcome,
    RelatedPartyReview,
    Severity,
    Subject,
    SubjectType,
)

# Roles that are always in scope regardless of ownership percentage.
_CONTROL_ROLES: frozenset[PartyRole] = frozenset(
    {PartyRole.DIRECTOR, PartyRole.CONTROLLER, PartyRole.AUTHORISED_SIGNATORY}
)
# SoW sub-case statuses that count as "cleared enough" for a source-of-funds individual.
_SOW_CLEARED: frozenset[CaseStatus] = frozenset(
    {CaseStatus.READY_FOR_REVIEW, CaseStatus.IN_REVIEW, CaseStatus.APPROVED}
)
_HIGH_MEDIA: frozenset[Severity] = frozenset({Severity.HIGH, Severity.CRITICAL})


def _slug(name: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in name.lower()).strip("-") or "party"


@dataclass(frozen=True, slots=True)
class RelatedPartyInput:
    """One party plus its screening and (optional) SoW sub-case status for the roll-up."""

    party: RelatedParty
    screening: PartyScreening
    sow_case_id: str | None = None
    sow_status: CaseStatus | None = None
    sow_coverage_pct: float = 0.0


@dataclass(frozen=True, slots=True)
class RelatedPartyService:
    """Pure, deterministic derivation + roll-up of key individuals."""

    ubo_threshold_pct: float = 25.0

    # ------------------------------------------------------------------ #
    def derive_from_ownership(self, ownership: OwnershipSummary | None) -> tuple[RelatedParty, ...]:
        """Map an `OwnershipSummary`'s beneficial owners into `RelatedParty` records."""
        if ownership is None:
            return ()
        out: list[RelatedParty] = []
        for o in ownership.owners:
            out.append(
                RelatedParty(
                    id=f"rp-{_slug(o.name)}",
                    subject=Subject(
                        id=_slug(o.name),
                        name=o.name,
                        type=SubjectType.INDIVIDUAL,
                        jurisdiction=o.country,
                    ),
                    role=PartyRole.BENEFICIAL_OWNER,
                    pct=o.pct,
                    # A controlling owner is treated as a source of the company's funds.
                    source_of_funds=o.pct >= self.ubo_threshold_pct,
                    citations=o.citations,
                )
            )
        return tuple(out)

    def in_scope(self, party: RelatedParty) -> bool:
        """UBOs/shareholders at/above the threshold, plus directors/controllers."""
        if party.role in _CONTROL_ROLES:
            return True
        if party.role in (PartyRole.BENEFICIAL_OWNER, PartyRole.SHAREHOLDER):
            return party.pct >= self.ubo_threshold_pct
        return False

    def assess(self, entries: Sequence[RelatedPartyInput]) -> RelatedPartyReview:
        """Roll up per-party CDD + SoW into a parent-level review (deterministic)."""
        outcomes = tuple(self._outcome(e) for e in entries)
        escalated = any(o.escalates for o in outcomes if o.in_scope)
        review = RelatedPartyReview(outcomes=outcomes, escalated=escalated, summary="")
        n = len(review.in_scope)
        esc = sum(1 for o in review.in_scope if o.escalates)
        summary = (
            f"{review.cleared_count}/{n} in-scope key individuals cleared; {esc} escalation(s)."
            if n
            else "No in-scope key individuals."
        )
        return RelatedPartyReview(outcomes=outcomes, escalated=escalated, summary=summary)

    # ------------------------------------------------------------------ #
    def _outcome(self, e: RelatedPartyInput) -> RelatedPartyOutcome:
        party, s = e.party, e.screening
        in_scope = self.in_scope(party)
        reasons: list[str] = []

        if s.sanctions_hit:
            reasons.append("sanctions hit")
        if s.is_pep:
            reasons.append("politically-exposed person")
        if s.adverse_media in _HIGH_MEDIA:
            reasons.append(f"{s.adverse_media.value} adverse media")
        if not s.identity_verified:
            reasons.append("identity not verified")

        sow_ok = True
        if party.source_of_funds:
            sow_ok = e.sow_status in _SOW_CLEARED
            if not sow_ok:
                status = e.sow_status.value if e.sow_status else "not started"
                reasons.append(f"source-of-funds SoW not cleared ({status})")

        cleared = (
            s.identity_verified
            and not s.sanctions_hit
            and s.adverse_media not in _HIGH_MEDIA
            and sow_ok
        )
        # A party escalates the parent only when it is in scope and not cleared
        # (or carries a hard signal). Soft: it raises the review bar, never blocks.
        escalates = in_scope and (
            not cleared or s.is_pep or s.sanctions_hit or s.adverse_media in _HIGH_MEDIA
        )
        return RelatedPartyOutcome(
            party=party,
            in_scope=in_scope,
            screening=s,
            sow_case_id=e.sow_case_id,
            sow_status=e.sow_status,
            sow_coverage_pct=round(e.sow_coverage_pct, 4),
            cleared=cleared,
            escalates=escalates,
            reasons=tuple(reasons),
        )


@dataclass(frozen=True, slots=True)
class RelatedPartyPolicy:
    """Soft maker-checker policy for the key-individuals roll-up (P-06)."""

    def requires_enhanced_review(self, review: RelatedPartyReview | None) -> bool:
        """True if any in-scope key individual escalates — enhanced review, not a block."""
        return bool(review and review.escalated)
