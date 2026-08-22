"""Local case-store adapter (CaseStorePort) — a WORKING offline SoW case store.

The ``local`` profile's stand-in for the durable, regional, CMEK-encrypted case store
(see ``docs/sow-longitudinal-audit-design.md`` §5). It keeps live ``SowCase`` aggregates
and sealed snapshots in-process, with the same versioned, optimistic-concurrency,
ACL-scoped, write-once-snapshot contract the orchestrator depends on — so the whole
long-running SoW workflow runs end to end with **no Google Cloud and no API key**.

Unlike the SQLite-backed local KB/audit, this store holds the aggregates as Python
objects rather than JSON rows: ``SowCase`` is a deep, frozen dataclass graph and the
repo ships ``to_jsonable`` but no inverse rehydrator, so in-process storage is the
faithful (and ``:memory:``-equivalent) offline choice. The managed gcp/platform adapters
— a regional document store — are the durable, cross-process target.
"""

from __future__ import annotations

from dataclasses import replace

from ...config import Settings
from ...domain.entitlements import case_acl_ok
from ...domain.errors import CaseAccessDeniedError, CaseNotFoundError, ConcurrencyError
from ...domain.models import SowCase, SowSnapshot


class LocalCaseStoreAdapter:
    """In-process, versioned, optimistic-concurrency case store (SDK-free)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._cases: dict[str, SowCase] = {}
        self._snapshots: dict[tuple[str, int], SowSnapshot] = {}

    def open(self, case: SowCase) -> SowCase:
        stored = replace(case, version=0)
        self._cases[case.id] = stored
        return stored

    def load(self, case_id: str, principals: tuple[str, ...]) -> SowCase:
        case = self._cases.get(case_id)
        if case is None:
            raise CaseNotFoundError(f"case '{case_id}' not found")
        if not case_acl_ok(case, principals):
            raise CaseAccessDeniedError(f"not entitled to case '{case_id}'")
        return case

    def save(self, case: SowCase, expected_version: int) -> SowCase:
        current = self._cases.get(case.id)
        if current is None:
            raise CaseNotFoundError(f"case '{case.id}' not found")
        if current.version != expected_version:
            raise ConcurrencyError(
                f"stale write to '{case.id}': expected v{expected_version}, "
                f"store has v{current.version}"
            )
        saved = replace(case, version=expected_version + 1)
        self._cases[case.id] = saved
        return saved

    def list_for(self, principals: tuple[str, ...]) -> list[SowCase]:
        return [c for c in self._cases.values() if case_acl_ok(c, principals)]

    def seal(self, snapshot: SowSnapshot) -> SowSnapshot:
        key = (snapshot.case_id, snapshot.version)
        if key in self._snapshots:
            raise ConcurrencyError(f"snapshot {key} already sealed (write-once)")
        self._snapshots[key] = snapshot
        return snapshot

    def get_snapshot(self, case_id: str, version: int) -> SowSnapshot:
        snap = self._snapshots.get((case_id, version))
        if snap is None:
            raise CaseNotFoundError(f"snapshot ({case_id}, v{version}) not found")
        return snap


class InMemoryCaseStore(LocalCaseStoreAdapter):
    """The local case store with a zero-arg constructor for tests and demo scripts.

    Ships in the package (not under ``tests/``) so the runnable demos need only
    ``PYTHONPATH=src``; the profile-bound adapter above keeps the one-``Settings``-arg
    construction convention, this convenience subclass just defaults it.
    """

    def __init__(self) -> None:
        super().__init__(Settings())
