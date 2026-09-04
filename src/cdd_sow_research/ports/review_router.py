"""ReviewRouterPort: the boundary that routes an escalated CDD dossier to human-review-console (rule
R8).

Every CDD dossier is consequential and always requires human review (maker-checker, P-06). The
dossier is proposed by the agent (the maker) and disposed by a qualified checker; rule R8 says a
producer that sets ``requires_human_review`` MUST route the item to the human-review-console
Human-Review & Maker-Checker Console rather than terminate the escalation in a per-repo boolean.
This port is that hand-off. The domain stays pure: the adapter (not this port) depends on the shared
``review-kit`` client and does the S2S submission.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..domain.models import CDDCase, PerpetualKycAssessment, UboResolution


@runtime_checkable
class ReviewRouterPort(Protocol):
    def route(self, case: CDDCase, *, maker: str) -> None:
        """Route an escalated dossier to human-review-console for human review (idempotent per case
        is ideal).
        """
        ...

    def route_monitoring(self, assessment: PerpetualKycAssessment, *, maker: str) -> None:
        """Route a perpetual-KYC assessment to human-review-console for human review (rule R8).

        A pKYC re-score is consequential in exactly the same way a dossier is: it changes
        the risk band a relationship is managed under. It therefore always sets
        ``requires_human_review`` and is routed here rather than acted on, so a checker
        disposes of the queue item under maker-checker (P-06).
        """
        ...

    def route_ownership(self, resolution: UboResolution, *, maker: str) -> None:
        """Route a UBO-graph resolution to human-review-console for human review (rule R8).

        A third verb rather than a reuse of ``route_monitoring``, because a resolution is
        a different consequential claim: it names the natural persons a bank will record
        as the beneficial owners of a customer, on a stated control basis, with structural
        indicators attached. The severity that earns is the OPACITY of the structure, not
        a queue priority, and the idempotency key is per subject and run date. Squeezing
        that through the pKYC verb would either mislabel the action in the console or
        invent a queue priority nothing computed.
        """
        ...
