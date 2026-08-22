"""Local sanctions provider (SanctionsListProviderPort) — snapshot file on disk.

The ``local`` profile's stand-in for the synced watchlist store: it reads a versioned
JSON snapshot from disk (a small **fictional** fixture bundled with the package by
default, refreshable for real data via ``scripts/sync_sanctions.py``). SDK-free and
deterministic, so the offline demo and unit tests screen against real-shaped entries.

Resolution order for the snapshot:

1. ``settings.local.sanctions_path`` (env ``CDD_LOCAL_SANCTIONS``), when set;
2. a synced snapshot at ``~/.cdd_sow_research/sanctions_snapshot.json``, when present;
3. the bundled fictional fixture.

Step 2 exists because screening a REAL subject against a fictional watchlist can only
ever return "no match": a clean result that means nothing. A ``live`` run that falls
through to the fixture says so loudly in the log rather than quietly reporting an
all-clear that was never a real check.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from pathlib import Path

from ...config import Settings
from ...domain.models import ListSource, SubjectType, WatchlistEntry

_LOG = logging.getLogger(__name__)

_BUNDLED = Path(__file__).with_name("data") / "sanctions_snapshot.json"
#: Where scripts/sync_sanctions.py writes the real merged OFAC / UN / EU / UK snapshot.
SYNCED_SNAPSHOT = Path.home() / ".cdd_sow_research" / "sanctions_snapshot.json"


class LocalSanctionsProviderAdapter:
    """Read a versioned watchlist snapshot JSON from disk."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._path = self._resolve_path(settings)
        self._cache: dict | None = None

    @classmethod
    def _resolve_path(cls, settings: Settings) -> Path:
        configured = getattr(getattr(settings, "local", None), "sanctions_path", "")
        if configured:
            return Path(configured)
        if SYNCED_SNAPSHOT.exists():
            return SYNCED_SNAPSHOT
        if settings.profile == "live":
            _LOG.warning(
                "screening a live case against the BUNDLED FICTIONAL watchlist: no real "
                "name can match it, so a clean screening result here means nothing. "
                "Refresh it with: python scripts/sync_sanctions.py --out %s",
                SYNCED_SNAPSHOT,
            )
        return _BUNDLED

    def _load(self) -> dict:
        if self._cache is None:
            self._cache = json.loads(self._path.read_text(encoding="utf-8"))
        return self._cache

    def version(self) -> str:
        return str(self._load().get("version", "unknown"))

    def iter_entries(self) -> Iterable[WatchlistEntry]:
        version = self.version()
        for raw in self._load().get("entries", []):
            yield WatchlistEntry(
                uid=str(raw.get("uid", "")),
                source=ListSource(raw.get("source", "ofac_sdn")),
                name=str(raw.get("name", "")),
                entity_type=SubjectType(raw.get("entity_type", "individual")),
                aliases=tuple(raw.get("aliases", []) or ()),
                dob=raw.get("dob"),
                countries=tuple(raw.get("countries", []) or ()),
                programs=tuple(raw.get("programs", []) or ()),
                remark=str(raw.get("remark", "")),
                list_version=version,
            )
