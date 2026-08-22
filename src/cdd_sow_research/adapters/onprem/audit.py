"""On-prem placeholder for ``AuditSinkPort`` — the sovereign target.

One of the reversibility (P-02, P-12) migration placeholders: in the managed profile
this port binds to the Cloud Logging locked-WORM-bucket adapter; switching ``profile``
to ``onprem`` rebinds it here. The adapter constructs cleanly with **no external
dependencies** and structurally satisfies the same Protocol as the managed adapter, so
the contract tests prove interface parity. ``record`` deliberately raises rather than
discarding the event: an unimplemented audit sink must never silently drop a CDD record
(rule R2), so porting on-premise *must* supply a real immutable (WORM) audit store.
Filling this body in is the only change required.
"""

from __future__ import annotations

from ...config import Settings
from ...domain.models import AuditEvent

_MESSAGE = (
    "On-prem AuditSinkPort adapter is a migration placeholder; implement against your "
    "on-premise platform. Core domain logic is unchanged."
)


class OnPremAuditAdapter:
    """Placeholder audit-sink adapter for the on-prem profile."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def record(self, event: AuditEvent) -> None:
        raise NotImplementedError(_MESSAGE)

    def record_once(self, event_id: str, event: AuditEvent) -> None:
        raise NotImplementedError(_MESSAGE)
