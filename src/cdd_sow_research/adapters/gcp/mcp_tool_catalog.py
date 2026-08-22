"""MCP tool-catalog adapter (ToolCatalogPort, A3, rule R4).

Exposes the governed, least-privilege tools B1 makes available over **MCP** (Model
Context Protocol 2025-11-25). For a standalone deployment this adapter serves a static,
in-process catalog describing the four CDD skills; in the full platform the governed MCP
catalog is hosted by the A3 registry. The tool schemas here are the contract surface a
peer agent sees.

This adapter imports no Google Cloud SDK; it is a thin in-process catalog.
"""

from __future__ import annotations

from ...config import Settings
from ...domain.models import ToolSpec

_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="assess_cdd",
        description="Build a full cited CDD dossier for a subject and its KYC documents.",
        input_schema={
            "type": "object",
            "properties": {
                "subject_name": {"type": "string"},
                "subject_type": {"type": "string", "enum": ["individual", "entity"]},
            },
            "required": ["subject_name"],
        },
    ),
    ToolSpec(
        name="build_source_of_wealth",
        description="Build a cited source-of-wealth narrative for a subject.",
        input_schema={
            "type": "object",
            "properties": {"subject_name": {"type": "string"}},
            "required": ["subject_name"],
        },
    ),
    ToolSpec(
        name="scan_adverse_media",
        description="Scan public-web adverse media for a subject name.",
        input_schema={
            "type": "object",
            "properties": {"subject_name": {"type": "string"}},
            "required": ["subject_name"],
        },
    ),
    ToolSpec(
        name="resolve_ownership",
        description="Resolve the corporate-ownership / UBO picture for an entity.",
        input_schema={
            "type": "object",
            "properties": {
                "entity_name": {"type": "string"},
                "jurisdiction": {"type": "string"},
            },
            "required": ["entity_name"],
        },
    ),
)


class McpToolCatalogAdapter:
    """Static governed MCP tool catalog for the standalone profile."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._tools = {tool.name: tool for tool in _TOOLS}

    def list_tools(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def get_tool(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)
