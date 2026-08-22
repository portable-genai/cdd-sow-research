"""On-prem placeholder for ``CorporateRegistryPort`` — the sovereign target.

One of the reversibility (P-02, P-12) migration placeholders: in the managed profile
this port binds to the grounded registry-lookup adapter; switching ``profile`` to
``onprem`` rebinds it here. The adapter constructs cleanly with **no external
dependencies** and structurally satisfies the same Protocol as the managed adapter, so
the contract tests prove interface parity. ``lookup`` deliberately raises rather than
returning a fabricated ownership tree: an unimplemented UBO resolver must never invent a
beneficial-ownership picture. Filling this body in is the only change required.
"""

from __future__ import annotations

from ...config import Settings
from ...domain.models import OwnershipSummary

_MESSAGE = (
    "On-prem CorporateRegistryPort adapter is a migration placeholder; implement against "
    "your on-premise platform. Core domain logic is unchanged."
)


class OnPremRegistryAdapter:
    """Placeholder corporate-registry adapter for the on-prem profile."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def lookup(self, entity_name: str, jurisdiction: str) -> OwnershipSummary:
        raise NotImplementedError(_MESSAGE)
