"""SanctionsListProviderPort — a versioned sanctions/PEP watchlist snapshot.

Screening reads a *synced, point-in-time* snapshot (not a live call to the publisher):
the ``version()`` is recorded on every alert so a hit is reproducible. The ``gcp`` adapter
reads a regional, CMEK-encrypted bucket kept current by a scheduled sync job (Cloud
Scheduler -> Cloud Run, pulling OFAC SLS + UN/EU/UK files); the ``local`` adapter reads a
bundled snapshot (refreshable via ``scripts/sync_sanctions.py``); the ``onprem`` entry is
a placeholder. The domain :class:`~cdd_sow_research.domain.screening.ScreeningService` does the
deterministic matching over the entries this port yields.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol, runtime_checkable

from ..domain.models import WatchlistEntry


@runtime_checkable
class SanctionsListProviderPort(Protocol):
    def version(self) -> str:
        """The current snapshot version (publish date / content hash) for audit."""
        ...

    def iter_entries(self) -> Iterable[WatchlistEntry]:
        """Yield every watchlist entry in the current snapshot."""
        ...
