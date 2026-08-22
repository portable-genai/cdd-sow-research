"""Local agent-registry adapter (AgentRegistryPort) — in-process A3 registry.

The ``local`` profile's stand-in for the **A3 Agent Registry**: a small in-process store
of A2A AgentCards, seedable and deterministic. Under ``local`` this platform client uses an
in-process implementation rather than HTTP to a sibling service (a laptop runs one app, not
the whole platform). When the Firestore emulator is opted in (``FIRESTORE_EMULATOR_HOST``
set AND the client lib imports), it routes to the emulator; the google client is imported
lazily, only on that branch.
"""

from __future__ import annotations

from ...config import Settings
from ...domain.models import AgentCard
from ._emulator import firestore_emulator_active


class LocalAgentRegistryAdapter:
    """In-process agent registry (Firestore-emulator-backed when opted in)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._cards: dict[str, AgentCard] = {}
        self._fs = None
        if firestore_emulator_active():
            from google.cloud import firestore  # noqa: PLC0415

            self._fs = firestore.Client(project=settings.project_id or "local")

    def register(self, card: AgentCard) -> None:
        if self._fs is not None:
            self._fs.collection("agents").document(card.name).set(
                {"name": card.name, "description": card.description, "url": card.url}
            )
        self._cards[card.name] = card

    def get(self, name: str) -> AgentCard | None:
        return self._cards.get(name)

    def list(self) -> list[AgentCard]:
        return list(self._cards.values())
