"""Human-in-the-loop (maker-checker) policy — General Principle P-06.

B1 is a *decision-support* agent, never an autonomous approver. P-06 (maker-checker)
requires that a human (an analyst / MLRO) reviews any consequential or low-assurance
output before it is relied upon. This module centralises that gate so the orchestrator
applies identical rules and the threshold is auditable in one place.

Policy (SPEC §5):
* A CDD dossier is consequential and **always** requires human review: a maker (the
  agent) proposes and a checker (a qualified human) disposes.
* A HIGH or PROHIBITED risk band, or any SANCTIONS / TERRORISM adverse-media hit,
  **escalates** the case (a stronger signal than the always-on review flag) so it is
  routed to enhanced review and the audit decision is ESCALATED.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import (
    AdverseMediaCategory,
    AdverseMediaFinding,
    RiskBand,
)
from .policy import EscalationPolicy

#: Risk bands that escalate a case to enhanced review (defaults; see EscalationPolicy).
_ESCALATING_BANDS: frozenset[RiskBand] = frozenset({RiskBand.HIGH, RiskBand.PROHIBITED})

#: Adverse-media categories that escalate a case regardless of the risk band.
_ESCALATING_MEDIA: frozenset[AdverseMediaCategory] = frozenset(
    {AdverseMediaCategory.SANCTIONS, AdverseMediaCategory.TERRORISM}
)


@dataclass(frozen=True, slots=True)
class CddReviewPolicy:
    """Maker-checker gate (P-06). Pure decision logic; no side effects."""

    #: Risk bands that escalate to enhanced review (bank-owned policy).
    escalating_bands: frozenset[RiskBand] = _ESCALATING_BANDS
    #: Adverse-media categories that escalate regardless of the band.
    escalating_media: frozenset[AdverseMediaCategory] = _ESCALATING_MEDIA

    @classmethod
    def from_policy(cls, policy: EscalationPolicy) -> CddReviewPolicy:
        """Build the gate from a bank-owned :class:`EscalationPolicy` (settings-driven)."""
        return cls(
            escalating_bands=frozenset(RiskBand(b) for b in policy.escalating_bands),
            escalating_media=frozenset(AdverseMediaCategory(m) for m in policy.escalating_media),
        )

    def requires_review(self) -> bool:
        """A CDD dossier always requires human review (it is consequential)."""
        return True

    def escalates(
        self,
        band: RiskBand,
        adverse_media: tuple[AdverseMediaFinding, ...] = (),
    ) -> bool:
        """Decide whether the case escalates to enhanced review.

        Escalates when the risk band is HIGH/PROHIBITED or any adverse-media finding is
        in a sanctions/terrorism category. Escalation only ever *raises* the review bar.

        Args:
            band: the overall risk band assigned to the case.
            adverse_media: the adverse-media findings attached to the case.

        Returns:
            True if the case must be routed to enhanced (escalated) review.
        """
        if band in self.escalating_bands:
            return True
        return any(finding.category in self.escalating_media for finding in adverse_media)
