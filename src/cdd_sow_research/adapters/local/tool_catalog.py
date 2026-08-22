"""Local tool-catalog adapter (ToolCatalogPort) — in-process MCP tool catalog.

The ``local`` profile's stand-in for the governed **MCP** tool catalog: a small,
deterministic in-process set of least-privilege tool specs. SDK-free and unconditional
(there is no emulator for the tool catalog).
"""

from __future__ import annotations

from ...config import Settings
from ...domain.models import ToolSpec


class LocalToolCatalogAdapter:
    """In-process catalog of the governed tools exposed to the agent."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._tools: dict[str, ToolSpec] = {
            "search_case_kb": ToolSpec(
                name="search_case_kb",
                description="Search the case's governed knowledge base.",
                input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
            )
        }

    def list_tools(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def get_tool(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)
