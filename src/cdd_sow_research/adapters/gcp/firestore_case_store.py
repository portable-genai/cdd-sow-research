"""GCP case-store adapter (CaseStorePort) : Firestore Native.

The managed, durable, cross-process home for long-running SoW cases (the ``local``
in-process store is the offline stand-in). A case is one ``sow_cases/{case_id}`` document
holding ``to_jsonable(SowCase)`` plus a top-level ``acl`` array and ``version``; sealed
snapshots live in a ``snapshots/{version}`` subcollection (write-once). The regional,
CMEK-encrypted database sits inside the VPC-SC perimeter (see ``infra/terraform/firestore.tf``).

Contract parity with the local store: subset ACL (fail-closed), optimistic concurrency via
a Firestore transaction (a stale version raises :class:`ConcurrencyError`, never a silent
overwrite), and write-once snapshots (a re-seal raises :class:`ConcurrencyError`). All
``google.cloud`` imports are lazy so the ``local`` / ``onprem`` / test profiles import this
module with **no** Google Cloud SDK installed.
"""

from __future__ import annotations

from typing import Any

from ...config import Settings
from ...domain.entitlements import case_acl, case_acl_ok
from ...domain.errors import CaseAccessDeniedError, CaseNotFoundError, ConcurrencyError
from ...domain.models import SowCase, SowSnapshot
from ...domain.serialization import (
    sow_case_from_jsonable,
    sow_snapshot_from_jsonable,
    to_jsonable,
)


class FirestoreCaseStoreAdapter:
    """Regional Firestore Native case store with ACL, optimistic concurrency, snapshots."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: Any | None = None

    # ------------------------------------------------------------------ #
    # Lazy client / references
    # ------------------------------------------------------------------ #
    def _db(self) -> Any:
        if self._client is None:
            from google.cloud import firestore  # lazy

            self._client = firestore.Client(
                project=self._settings.project_id,
                database=self._settings.case_store.database,
            )
        return self._client

    def _cases(self) -> Any:
        return self._db().collection(self._settings.case_store.collection)

    def _doc(self, case_id: str) -> Any:
        return self._cases().document(case_id)

    # ------------------------------------------------------------------ #
    # CaseStorePort
    # ------------------------------------------------------------------ #
    def open(self, case: SowCase) -> SowCase:
        from dataclasses import replace

        stored = replace(case, version=0)
        self._doc(stored.id).set(self._to_document(stored))
        return stored

    def load(self, case_id: str, principals: tuple[str, ...]) -> SowCase:
        snap = self._doc(case_id).get()
        if not snap.exists:
            raise CaseNotFoundError(f"case '{case_id}' not found")
        case = sow_case_from_jsonable(snap.to_dict()["case"])
        if not case_acl_ok(case, principals):
            raise CaseAccessDeniedError(f"not entitled to case '{case_id}'")
        return case

    def save(self, case: SowCase, expected_version: int) -> SowCase:
        from dataclasses import replace

        from google.api_core.exceptions import Aborted  # lazy

        saved = replace(case, version=expected_version + 1)
        doc = self._doc(case.id)
        transaction = self._db().transaction()

        @self._db().transactional  # type: ignore[misc]
        def _txn(txn: Any) -> None:
            snap = doc.get(transaction=txn)
            if not snap.exists:
                raise CaseNotFoundError(f"case '{case.id}' not found")
            # Compare INSIDE the transaction so a retry re-reads the current version.
            current = int(snap.to_dict().get("version", -1))
            if current != expected_version:
                raise ConcurrencyError(
                    f"stale write to '{case.id}': expected v{expected_version}, "
                    f"store has v{current}"
                )
            txn.set(doc, self._to_document(saved))

        try:
            _txn(transaction)
        except Aborted as exc:  # transaction gave up after contention retries
            raise ConcurrencyError(f"case '{case.id}' write aborted under contention") from exc
        return saved

    def list_for(self, principals: tuple[str, ...]) -> list[SowCase]:
        from google.cloud.firestore_v1 import FieldFilter  # lazy

        tags = [p for p in principals if p.startswith(("case:", "tenant:"))] or list(principals)
        query = self._cases()
        if tags:
            # array_contains_any pre-filters to cases sharing at least one tag; the subset
            # check below is the authoritative fail-closed filter.
            query = query.where(filter=FieldFilter("acl", "array_contains_any", tags[:10]))
        out: list[SowCase] = []
        for snap in query.stream():
            case = sow_case_from_jsonable(snap.to_dict()["case"])
            if case_acl_ok(case, principals):
                out.append(case)
        return out

    def seal(self, snapshot: SowSnapshot) -> SowSnapshot:
        from google.api_core.exceptions import AlreadyExists  # lazy

        ref = self._doc(snapshot.case_id).collection("snapshots").document(str(snapshot.version))
        try:
            ref.create(to_jsonable(snapshot))  # fail-if-exists -> write-once
        except AlreadyExists as exc:
            raise ConcurrencyError(
                f"snapshot ({snapshot.case_id}, v{snapshot.version}) already sealed (write-once)"
            ) from exc
        return snapshot

    def get_snapshot(self, case_id: str, version: int) -> SowSnapshot:
        ref = self._doc(case_id).collection("snapshots").document(str(version))
        snap = ref.get()
        if not snap.exists:
            raise CaseNotFoundError(f"snapshot ({case_id}, v{version}) not found")
        return sow_snapshot_from_jsonable(snap.to_dict())

    # ------------------------------------------------------------------ #
    # Mapping
    # ------------------------------------------------------------------ #
    @staticmethod
    def _to_document(case: SowCase) -> dict[str, Any]:
        """The stored shape: the jsonable case plus top-level ``acl`` + ``version`` so the
        adapter can ACL-filter and version-compare without rehydrating first."""
        return {
            "case": to_jsonable(case),
            "acl": list(case_acl(case)),
            "version": case.version,
        }
