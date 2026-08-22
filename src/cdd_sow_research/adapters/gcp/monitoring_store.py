"""GCP monitoring-store adapter (MonitoringStorePort): Firestore Native.

The managed, durable, cross-process home for perpetual-KYC baselines and the review queue
(the ``local`` in-process store is the offline stand-in). A baseline is one
``pkyc_baselines/{subject_id}`` document and the current assessment for a subject is one
``pkyc_assessments/{subject_id}`` document; both hold ``to_jsonable(...)`` plus a top-level
``acl`` array so the fail-closed ACL match is enforced on read exactly as it is offline.
The regional, CMEK-encrypted database sits inside the VPC-SC perimeter (see
``infra/terraform/firestore.tf``).

Contract parity with the local store: subset ACL on the baseline (a cross-tenant reader
gets :class:`CaseAccessDeniedError`, never another bank's history) and a tenant-tag match
on the queue listing. All ``google.cloud`` imports are lazy, so the ``local`` / ``onprem``
/ test profiles import this module with **no** Google Cloud SDK installed.
"""

from __future__ import annotations

from typing import Any

from ...config import Settings
from ...domain.entitlements import queue_acl_ok
from ...domain.errors import CaseAccessDeniedError
from ...domain.models import PerpetualKycAssessment, PerpetualKycBaseline
from ...domain.serialization import (
    perpetual_kyc_assessment_from_jsonable,
    perpetual_kyc_baseline_from_jsonable,
    to_jsonable,
)
from ..local.monitoring_store import queue_sort_key


class FirestoreMonitoringStoreAdapter:
    """Regional Firestore Native perpetual-KYC store with fail-closed ACL scoping."""

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
                database=self._settings.monitoring.database or self._settings.case_store.database,
            )
        return self._client

    def _baseline_doc(self, subject_id: str) -> Any:
        return (
            self._db()
            .collection(self._settings.monitoring.baselines_collection)
            .document(subject_id)
        )

    def _assessment_doc(self, subject_id: str) -> Any:
        return (
            self._db()
            .collection(self._settings.monitoring.assessments_collection)
            .document(subject_id)
        )

    # ------------------------------------------------------------------ #
    # MonitoringStorePort
    # ------------------------------------------------------------------ #
    def load_baseline(
        self, subject_id: str, principals: tuple[str, ...]
    ) -> PerpetualKycBaseline | None:
        snap = self._baseline_doc(subject_id).get()
        if not snap.exists:
            return None
        payload = snap.to_dict() or {}
        baseline = perpetual_kyc_baseline_from_jsonable(payload["baseline"])
        if not set(baseline.acl) <= set(principals):
            raise CaseAccessDeniedError(
                f"not entitled to the perpetual-KYC baseline for '{subject_id}'"
            )
        return baseline

    def save_baseline(self, baseline: PerpetualKycBaseline) -> PerpetualKycBaseline:
        self._baseline_doc(baseline.subject_id).set(
            {"acl": list(baseline.acl), "baseline": to_jsonable(baseline)}
        )
        return baseline

    def record(self, assessment: PerpetualKycAssessment) -> PerpetualKycAssessment:
        item = assessment.queue_item
        self._assessment_doc(assessment.subject_id).set(
            {
                "acl": list(assessment.acl),
                "tenant": assessment.tenant,
                "priority": item.priority.value if item is not None else "standard",
                "sla_due": item.sla_due if item is not None else "",
                "assessment": to_jsonable(assessment),
            }
        )
        return assessment

    def queue(self, principals: tuple[str, ...]) -> tuple[PerpetualKycAssessment, ...]:
        tenants = sorted(p.removeprefix("tenant:") for p in principals if p.startswith("tenant:"))
        if not tenants:
            return ()  # fail closed: no tenant tag, no queue
        collection = self._db().collection(self._settings.monitoring.assessments_collection)
        out: list[PerpetualKycAssessment] = []
        for tenant in tenants:
            for snap in collection.where("tenant", "==", tenant).stream():
                payload = snap.to_dict() or {}
                acl = tuple(str(t) for t in payload.get("acl") or ())
                # Re-check server side: the query narrows, the ACL rule decides.
                if not queue_acl_ok(acl, principals):
                    continue
                out.append(perpetual_kyc_assessment_from_jsonable(payload["assessment"]))
        return tuple(sorted(out, key=queue_sort_key))
