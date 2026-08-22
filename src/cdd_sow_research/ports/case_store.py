"""CaseStorePort — durable persistence for long-running SoW cases.

The one outbound concern the longitudinal SoW flow adds (see
``docs/sow-longitudinal-audit-design.md`` §8). A case is a versioned aggregate that
lives for weeks; writes are optimistic (the caller passes the version it read), and
approved analyses are sealed as immutable snapshots. Like every other port this is a
``@runtime_checkable`` Protocol bound per profile: the ``gcp`` adapter is a regional,
CMEK-encrypted document store inside the VPC-SC perimeter; the ``onprem`` entry is a
placeholder stub; tests/demos use an in-memory implementation.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..domain.models import SowCase, SowSnapshot


@runtime_checkable
class CaseStorePort(Protocol):
    def open(self, case: SowCase) -> SowCase:
        """Persist a freshly opened case (version 0) and return it."""
        ...

    def load(self, case_id: str, principals: tuple[str, ...]) -> SowCase:
        """Load a case for the given ACL principals (raises if absent/forbidden)."""
        ...

    def save(self, case: SowCase, expected_version: int) -> SowCase:
        """Persist a mutated case iff its stored version matches ``expected_version``."""
        ...

    def list_for(self, principals: tuple[str, ...]) -> list[SowCase]:
        """List cases visible to the given ACL principals."""
        ...

    def seal(self, snapshot: SowSnapshot) -> SowSnapshot:
        """Write an immutable approved snapshot (write-once)."""
        ...

    def get_snapshot(self, case_id: str, version: int) -> SowSnapshot:
        """Load a previously sealed snapshot."""
        ...
