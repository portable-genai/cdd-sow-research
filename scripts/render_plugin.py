#!/usr/bin/env python3
"""Render Doc1's Agent Plugins 1.0.0 directory from what this repo already declares.

Nothing here is hand-authored. The manifest's identity comes from the A2A agent card this repo
already publishes, its keywords come from the governed tool catalog, and ``skills/`` is copied
from ``.agents/skills``. A manifest typed out by hand would be a second description of the
service, and a second description is one that can be wrong.

Agent Plugins packages TOOLING and carries no data-portability mechanism, so nothing here
touches the evidence trail: the ledger keeps its own export format and its adapters, and a
plugin only ever REACHES it through the kit's read-only tools.

Run it with ``make plugin``; the output is build output and is not committed.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

from hex_service_kit.plugin import (
    Author,
    PluginSpec,
    StdioServer,
    keywords_from_skill_ids,
    render,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_DEST = REPO_ROOT / "dist" / "plugin"


def build_spec() -> PluginSpec:
    """Assemble the spec from this repo's own declarations, never from literals."""
    from cdd_sow_research.adapters.gcp.mcp_tool_catalog import McpToolCatalogAdapter
    from cdd_sow_research.agent.agent_card import agent_card_document
    from cdd_sow_research.config import Settings

    settings = Settings.load()
    card = agent_card_document(settings)
    catalog = McpToolCatalogAdapter(settings)

    return PluginSpec(
        name="cdd-sow-research",
        version=str(card.get("version") or "0.0.1"),
        description=str(card.get("description") or ""),
        license="Apache-2.0",
        repository="https://github.com/portable-genai/cdd-sow-research",
        # The card's skills are CAPABILITIES and reach a client as MCP tools through mcp.json,
        # not as files. They land in the manifest only as keywords.
        keywords=keywords_from_skill_ids([spec.name for spec in catalog.list_tools()]),
        author=Author(name="portable-genai"),
        servers={
            "cdd-sow-research": StdioServer(
                command="python",
                args=("-m", "cdd_sow_research.mcp"),
                cwd="${PLUGIN_ROOT}",
            )
        },
        skills_source=REPO_ROOT / ".agents" / "skills",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dest", type=pathlib.Path, default=DEFAULT_DEST)
    args = parser.parse_args(argv)
    report = render(build_spec(), args.dest)
    print(f"rendered {report.root}: {len(report.skills)} skills, {len(report.servers)} server(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
