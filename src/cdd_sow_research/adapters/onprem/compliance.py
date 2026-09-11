"""On-prem placeholder for ``ComplianceClientPort`` — the sovereign target.

One of the reversibility (P-02, P-12) migration placeholders: in the platform profile
this port binds to the `compliance-advisory` HTTP client; switching ``profile`` to
``onprem`` rebinds it here. The adapter constructs cleanly with **no external
dependencies** and structurally satisfies the same Protocol as the managed adapter, so
the contract tests prove interface parity. ``check`` deliberately raises rather than
returning a vacuous "no concerns" answer: an unimplemented regulatory check must never
silently wave a dossier through. Filling this body in is the only change required.
"""

from __future__ import annotations

from ...config import Settings
from ...domain.models import ComplianceAnswer

_MESSAGE = (
    "On-prem ComplianceClientPort adapter is a migration placeholder; implement against "
    "your on-premise platform. Core domain logic is unchanged."
)


class OnPremComplianceAdapter:
    """Placeholder compliance-client adapter for the on-prem profile."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def check(self, question: str, actor: str) -> ComplianceAnswer:
        raise NotImplementedError(_MESSAGE)
