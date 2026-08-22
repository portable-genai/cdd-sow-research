"""GCP sanctions provider (SanctionsListProviderPort) — synced regional bucket.

Reads the **point-in-time watchlist snapshot** from a regional, CMEK-encrypted Cloud
Storage bucket (``settings.sanctions.bucket`` / ``object_name``) that a scheduled sync job
keeps current — Cloud Scheduler triggers a Cloud Run job that pulls OFAC SLS (SDN +
Consolidated) plus the UN/EU/UK lists, diffs, and writes the merged JSON snapshot
(``scripts/sync_sanctions.py``; infra in ``infra/terraform/sanctions_sync.tf``). Screening
reads this cached copy, never the publishers directly, so alerts are reproducible and
stay in-region with no per-request egress.

The Cloud Storage SDK import is lazy so the local/on-prem/test profiles import without it.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from ...config import Settings
from ...domain.models import ListSource, SubjectType, WatchlistEntry


class GcsSanctionsProviderAdapter:
    """Read the synced watchlist snapshot object from the regional CMEK bucket."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._bucket = settings.sanctions.bucket
        self._object = settings.sanctions.object_name
        self._client: Any | None = None
        self._cache: dict | None = None

    def _load(self) -> dict:
        if self._cache is not None:
            return self._cache
        from google.cloud import storage  # lazy import (GCP SDK only on this path)

        if self._client is None:
            self._client = storage.Client(project=self._settings.project_id)
        blob = self._client.bucket(self._bucket).blob(self._object)
        self._cache = json.loads(blob.download_as_text())
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
