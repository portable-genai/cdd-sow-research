"""Governance ports — the A3 Agent Registry concern and the MCP tool catalog (R4).

Primary GCP adapters: an **A2A AgentCard** published at
``/.well-known/agent-card.json`` (with a ``platform`` client to the
``agent-registry`` service), and a governed, least-privilege **MCP** tool catalog.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..domain.models import AgentCard, ToolSpec


@runtime_checkable
class AgentRegistryPort(Protocol):
    def register(self, card: AgentCard) -> None: ...

    def get(self, name: str) -> AgentCard | None: ...

    def list(self) -> list[AgentCard]: ...


@runtime_checkable
class ToolCatalogPort(Protocol):
    def list_tools(self) -> list[ToolSpec]: ...

    def get_tool(self, name: str) -> ToolSpec | None: ...
