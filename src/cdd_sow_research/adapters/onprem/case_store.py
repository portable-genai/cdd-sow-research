"""On-prem placeholder for ``CaseStorePort`` — the sovereign target.

A reversibility (P-02, P-12) migration placeholder: in the managed profile this port
binds to a regional, CMEK-encrypted document store inside the VPC-SC perimeter; switching
``profile`` to ``onprem`` rebinds it here. The adapter constructs cleanly with **no
external dependencies** and structurally satisfies the same Protocol as the managed
adapter, so the contract tests prove interface parity. Every method raises rather than
returning empty: an unimplemented case store must never silently lose a long-running SoW
case or its sealed snapshots. Porting on-premise *must* supply a real durable, versioned,
ACL-scoped store; filling these bodies in is the only change required.

For tests and the runnable demo, use the in-memory implementation in
``tests/fixtures`` rather than this placeholder.
"""

from __future__ import annotations

from ...config import Settings
from ...domain.models import SowCase, SowSnapshot

_MESSAGE = (
    "On-prem CaseStorePort adapter is a migration placeholder; implement against your "
    "on-premise durable, versioned, ACL-scoped case store. Core domain logic is unchanged."
)


class OnPremCaseStoreAdapter:
    """Placeholder case-store adapter for the on-prem profile."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def open(self, case: SowCase) -> SowCase:
        raise NotImplementedError(_MESSAGE)

    def load(self, case_id: str, principals: tuple[str, ...]) -> SowCase:
        raise NotImplementedError(_MESSAGE)

    def save(self, case: SowCase, expected_version: int) -> SowCase:
        raise NotImplementedError(_MESSAGE)

    def list_for(self, principals: tuple[str, ...]) -> list[SowCase]:
        raise NotImplementedError(_MESSAGE)

    def seal(self, snapshot: SowSnapshot) -> SowSnapshot:
        raise NotImplementedError(_MESSAGE)

    def get_snapshot(self, case_id: str, version: int) -> SowSnapshot:
        raise NotImplementedError(_MESSAGE)
