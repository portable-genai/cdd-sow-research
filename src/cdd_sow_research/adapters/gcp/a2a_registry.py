"""A2A AgentCard registry adapter (AgentRegistryPort, A3, rule R4).

Serves and resolves B1's A2A AgentCard locally (the same minimal A2A shape the
``agent-registry`` service stores). In a standalone deployment the card is published
at ``/.well-known/agent-card.json`` by the API; this adapter holds an in-process registry
of cards so the agent can self-register and resolve peers without the platform registry.
The ``platform`` adapter delegates to the shared A3 service over HTTP.

This adapter imports no Google Cloud SDK; it is a thin in-process registry.
"""

from __future__ import annotations

from ...config import Settings
from ...domain.models import AgentCard


class A2ARegistryAdapter:
    """In-process A2A AgentCard registry for the standalone profile."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._cards: dict[str, AgentCard] = {}

    def register(self, card: AgentCard) -> None:
        self._cards[card.name] = card

    def get(self, name: str) -> AgentCard | None:
        return self._cards.get(name)

    def list(self) -> list[AgentCard]:
        return list(self._cards.values())
