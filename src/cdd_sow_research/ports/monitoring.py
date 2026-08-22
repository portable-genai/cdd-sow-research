"""MonitoringStorePort: durable perpetual-KYC baselines and the explainable review queue.

Perpetual KYC is a *delta* control: it needs to remember what the last accepted picture
was (the baseline) so the next run can tell movement from noise, and it needs to keep the
resulting assessments so an analyst can open a review queue without re-running anything.
That is the one outbound concern the module adds, and it is this port.

Object-level authorization lives at this boundary, fail-closed and server-verified: every
record carries the server-derived ACL tags from
:func:`~cdd_sow_research.domain.entitlements.case_tags` (``case:<subject id>`` plus
``tenant:<tenant>``) and a reader must hold all of them, so an authenticated caller in
another tenant gets :class:`~cdd_sow_research.domain.errors.CaseAccessDeniedError` (HTTP 403)
rather than another bank's monitoring history. Queue listing is scoped by tenant tag with
the same fail-closed rule (an untagged record is never listed).

Bound per profile like every other port: ``gcp``/``platform`` use the regional,
CMEK-encrypted Firestore database inside the VPC-SC perimeter, ``local``/``live`` use the
WORKING in-process store (the same choice the SoW case store makes offline, and for the
same reason: these are deep frozen dataclass graphs), and ``onprem`` is the fail-fast
sovereign placeholder.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..domain.models import PerpetualKycAssessment, PerpetualKycBaseline


@runtime_checkable
class MonitoringStorePort(Protocol):
    def load_baseline(
        self, subject_id: str, principals: tuple[str, ...]
    ) -> PerpetualKycBaseline | None:
        """The stored baseline for a subject, or ``None`` if this is the first run.

        Raises ``CaseAccessDeniedError`` when a baseline exists but the caller does not
        hold every one of its ACL tags (never a 404-shaped answer that leaks existence).
        """
        ...

    def save_baseline(self, baseline: PerpetualKycBaseline) -> PerpetualKycBaseline:
        """Persist the baseline this run establishes for the next one."""
        ...

    def record(self, assessment: PerpetualKycAssessment) -> PerpetualKycAssessment:
        """Persist one perpetual-KYC assessment so it appears in the review queue."""
        ...

    def queue(self, principals: tuple[str, ...]) -> tuple[PerpetualKycAssessment, ...]:
        """The caller's review queue, most urgent first, scoped to their tenant tag."""
        ...
