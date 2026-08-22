"""On-prem placeholder for ``OwnershipGraphPort`` — the sovereign target.

One of the reversibility (P-02, P-12) migration placeholders: in the managed profile this
port binds to the grounded one-hop registry adapter; switching ``profile`` to ``onprem``
rebinds it here. The adapter constructs cleanly with **no external dependencies** and
structurally satisfies the same Protocol as the managed adapter, so the contract tests
prove interface parity.

``hop`` deliberately RAISES rather than returning an unresolved hop. The port's contract
says an unresolved hop means "the registry has no record", which the engine treats as an
opacity indicator and carries forward; returning that from an unimplemented adapter would
turn a missing integration into a finding about the customer's structure, and every
percentage below it would silently understate the truth. An unimplemented UBO resolver
must never invent, and must never let its own absence read as evidence. Filling this body
in is the only change required.
"""

from __future__ import annotations

from ...config import Settings
from ...domain.models import RegistryHop

_MESSAGE = (
    "On-prem OwnershipGraphPort adapter is a migration placeholder; implement against your "
    "on-premise corporate-registry source. Core domain logic is unchanged."
)


class OnPremOwnershipGraphAdapter:
    """Placeholder ownership-graph adapter for the on-prem profile."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def hop(self, entity_name: str, jurisdiction: str) -> RegistryHop:
        raise NotImplementedError(_MESSAGE)
