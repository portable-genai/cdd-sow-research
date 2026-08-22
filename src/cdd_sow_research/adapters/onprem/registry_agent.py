"""On-prem placeholder for ``AgentRegistryPort`` — the sovereign target.

One of the reversibility (P-02, P-12) migration placeholders: in the managed profile
this port binds to the A2A AgentCard registry adapter; switching ``profile`` to
``onprem`` rebinds it here. The adapter constructs cleanly with **no external
dependencies** and structurally satisfies the same Protocol as the managed adapter, so
the contract tests prove interface parity. Porting the agent registry to an on-premise
catalog is *only* a matter of filling these bodies in: the domain is unchanged.
"""

from __future__ import annotations

from ...config import Settings
from ...domain.models import AgentCard

_MESSAGE = (
    "On-prem AgentRegistryPort adapter is a migration placeholder; implement against your "
    "on-premise platform. Core domain logic is unchanged."
)


class OnPremAgentRegistryAdapter:
    """Placeholder agent-registry adapter for the on-prem profile."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def register(self, card: AgentCard) -> None:
        raise NotImplementedError(_MESSAGE)

    def get(self, name: str) -> AgentCard | None:
        raise NotImplementedError(_MESSAGE)

    def list(self) -> list[AgentCard]:
        raise NotImplementedError(_MESSAGE)
