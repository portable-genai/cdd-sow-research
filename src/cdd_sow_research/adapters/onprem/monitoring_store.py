"""On-prem placeholder for ``MonitoringStorePort`` — the sovereign target.

A reversibility (P-02, P-12) migration placeholder: in the managed profile this port binds
to a regional, CMEK-encrypted Firestore database inside the VPC-SC perimeter; switching
``profile`` to ``onprem`` rebinds it here. The adapter constructs cleanly with **no
external dependencies** and structurally satisfies the same Protocol as the managed
adapter, so the contract tests prove interface parity.

Every method raises rather than returning an empty answer. A silently empty baseline would
make every observed signal look NEW and a silently empty queue would hide escalations that
a checker must see, so an unimplemented perpetual-KYC store must fail loudly. Porting
on-premise *must* supply a real durable, ACL-scoped store; filling these bodies in is the
only change required.
"""

from __future__ import annotations

from ...config import Settings
from ...domain.models import PerpetualKycAssessment, PerpetualKycBaseline

_MESSAGE = (
    "On-prem MonitoringStorePort adapter is a migration placeholder; implement against your "
    "on-premise durable, ACL-scoped perpetual-KYC store. Core domain logic is unchanged."
)


class OnPremMonitoringStoreAdapter:
    """Placeholder perpetual-KYC store adapter for the on-prem profile."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def load_baseline(
        self, subject_id: str, principals: tuple[str, ...]
    ) -> PerpetualKycBaseline | None:
        raise NotImplementedError(_MESSAGE)

    def save_baseline(self, baseline: PerpetualKycBaseline) -> PerpetualKycBaseline:
        raise NotImplementedError(_MESSAGE)

    def record(self, assessment: PerpetualKycAssessment) -> PerpetualKycAssessment:
        raise NotImplementedError(_MESSAGE)

    def queue(self, principals: tuple[str, ...]) -> tuple[PerpetualKycAssessment, ...]:
        raise NotImplementedError(_MESSAGE)
