"""On-prem placeholder for ``SanctionsListProviderPort`` — the sovereign target.

A reversibility (P-02, P-12) migration placeholder: in the managed profile this port reads
the regional, CMEK-encrypted watchlist bucket synced from the publishers; switching
``profile`` to ``onprem`` rebinds it here. It constructs cleanly with no external
dependencies and satisfies the same Protocol, so contract tests prove parity, but every
method raises rather than returning empty: an unimplemented screening source must never
silently pass a designated party. Porting on-premise supplies a real synced list store.
"""

from __future__ import annotations

from collections.abc import Iterable

from ...config import Settings
from ...domain.models import WatchlistEntry

_MESSAGE = (
    "On-prem SanctionsListProviderPort adapter is a migration placeholder; implement "
    "against your on-premise synced watchlist store. Core domain logic is unchanged."
)


class OnPremSanctionsProviderAdapter:
    """Placeholder sanctions provider for the on-prem profile."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def version(self) -> str:
        raise NotImplementedError(_MESSAGE)

    def iter_entries(self) -> Iterable[WatchlistEntry]:
        raise NotImplementedError(_MESSAGE)
