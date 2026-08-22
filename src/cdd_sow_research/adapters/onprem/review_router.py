"""On-prem placeholder for ``ReviewRouterPort`` — the sovereign target.

One of the reversibility (P-02, P-12) migration placeholders: in the managed profile this port
binds to the Hrz7 submission adapter; switching ``profile`` to ``onprem`` rebinds it here. The
adapter constructs cleanly with **no external dependencies** and structurally satisfies the same
Protocol as the managed adapter, so the contract tests prove interface parity. ``route``
deliberately raises rather than silently dropping the escalation: an unimplemented router must
never let a consequential dossier bypass human review (P-06, rule R8), so porting on-premise
*must* supply a real hand-off to the sovereign review console. Filling this body in is the only
change required.
"""

from __future__ import annotations

from ...config import Settings
from ...domain.models import CDDCase, PerpetualKycAssessment, UboResolution

_MESSAGE = (
    "On-prem ReviewRouterPort adapter is a migration placeholder; implement against your "
    "on-premise human-review console. Core domain logic is unchanged."
)


class OnPremReviewRouter:
    """Placeholder review-router adapter for the on-prem profile."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def route(self, case: CDDCase, *, maker: str) -> None:
        raise NotImplementedError(_MESSAGE)

    def route_monitoring(self, assessment: PerpetualKycAssessment, *, maker: str) -> None:
        raise NotImplementedError(_MESSAGE)

    def route_ownership(self, resolution: UboResolution, *, maker: str) -> None:
        raise NotImplementedError(_MESSAGE)
