"""Local monitoring-store adapter (MonitoringStorePort) — a WORKING offline pKYC store.

The ``local`` profile's stand-in for the durable, regional, CMEK-encrypted perpetual-KYC
store. It keeps baselines and assessments in-process with the SAME fail-closed ACL
contract the managed adapter enforces, so the whole perpetual-KYC journey (detect,
re-score, queue, route to Hrz7) runs end to end with **no Google Cloud and no API key**.

In-process rather than SQLite for the same reason the local SoW case store is:
``PerpetualKycAssessment`` is a deep frozen dataclass graph, and holding the aggregates as
Python objects is the faithful offline choice. The managed Firestore adapter is the
durable, cross-process target.
"""

from __future__ import annotations

from ...config import Settings
from ...domain.entitlements import queue_acl_ok
from ...domain.errors import CaseAccessDeniedError
from ...domain.models import PerpetualKycAssessment, PerpetualKycBaseline

#: Queue ordering: most urgent first, then soonest SLA, then subject id (stable).
_PRIORITY_ORDER: dict[str, int] = {"urgent": 0, "high": 1, "standard": 2, "low": 3}


def queue_sort_key(assessment: PerpetualKycAssessment) -> tuple[int, str, str]:
    """The deterministic review-queue ordering shared by every store adapter."""
    item = assessment.queue_item
    priority = item.priority.value if item is not None else "standard"
    sla_due = item.sla_due if item is not None else ""
    return (_PRIORITY_ORDER.get(priority, 9), sla_due, assessment.subject_id)


class LocalMonitoringStoreAdapter:
    """In-process, ACL-scoped perpetual-KYC baseline + queue store (SDK-free)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._baselines: dict[str, PerpetualKycBaseline] = {}
        self._assessments: dict[str, PerpetualKycAssessment] = {}

    def load_baseline(
        self, subject_id: str, principals: tuple[str, ...]
    ) -> PerpetualKycBaseline | None:
        baseline = self._baselines.get(subject_id)
        if baseline is None:
            return None
        if not set(baseline.acl) <= set(principals):
            raise CaseAccessDeniedError(
                f"not entitled to the perpetual-KYC baseline for '{subject_id}'"
            )
        return baseline

    def save_baseline(self, baseline: PerpetualKycBaseline) -> PerpetualKycBaseline:
        self._baselines[baseline.subject_id] = baseline
        return baseline

    def record(self, assessment: PerpetualKycAssessment) -> PerpetualKycAssessment:
        # One live queue entry per subject: the newest assessment supersedes the previous
        # one (the superseded run stays in the WORM audit trail, not in the queue).
        self._assessments[assessment.subject_id] = assessment
        return assessment

    def queue(self, principals: tuple[str, ...]) -> tuple[PerpetualKycAssessment, ...]:
        visible = [a for a in self._assessments.values() if queue_acl_ok(a.acl, principals)]
        return tuple(sorted(visible, key=queue_sort_key))


class InMemoryMonitoringStore(LocalMonitoringStoreAdapter):
    """The local monitoring store with a zero-arg constructor for tests and demos."""

    def __init__(self) -> None:
        super().__init__(Settings())
