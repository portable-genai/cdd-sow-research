"""The declared tool catalog is now SERVED, and the plugin is rendered from the declarations.

cdd-sow-research declared its capability surface twice over, as an A2A agent card and as a governed
tool catalog of JSON Schemas, and served neither. There was no MCP server process anywhere in the
fleet, so a surface described in two places could be read by a human and reached by nobody.

These guards are about the seam rather than the transport. What can go wrong here is not that
MCP breaks; it is that the served surface and the declared surface drift apart, so the catalog
says one thing and the process does another. `bind` refuses that in both directions and these
tests pin both directions with it.

The MCP SDK is in the `[gcp]` extra and is not installed by the offline gate, so everything
below uses `bind`, which is pure. Starting a server is a managed-profile concern; agreeing with
the catalog is not, and it is the half that can silently rot.
"""

from __future__ import annotations

import json
import pathlib

import jsonschema
import pytest
from hex_service_kit.mcpserve import ToolDispatchError, bind

from cdd_sow_research.adapters.gcp.mcp_tool_catalog import McpToolCatalogAdapter
from cdd_sow_research.config import Settings
from cdd_sow_research.mcp import server as mcp_server


@pytest.fixture
def catalog() -> McpToolCatalogAdapter:
    return McpToolCatalogAdapter(Settings.load())


def test_every_declared_tool_has_a_handler_and_no_handler_is_undeclared(
    catalog: McpToolCatalogAdapter,
) -> None:
    """The whole point of binding at start-up rather than on the first call."""
    bound = bind(catalog, mcp_server.build_handlers(actor="svc:test"))

    assert set(bound) == {spec.name for spec in catalog.list_tools()}


def test_a_declared_tool_with_no_handler_refuses_to_start(
    catalog: McpToolCatalogAdapter,
) -> None:
    """A capability the service advertises and cannot perform must not be served."""
    handlers = mcp_server.build_handlers(actor="svc:test")
    del handlers["build_source_of_wealth"]

    with pytest.raises(ToolDispatchError, match="no handler"):
        bind(catalog, handlers)


def test_a_handler_for_an_undeclared_tool_refuses_to_start(
    catalog: McpToolCatalogAdapter,
) -> None:
    """An ungoverned entry point is the more dangerous direction of the same mismatch."""
    handlers = mcp_server.build_handlers(actor="svc:test")
    handlers["exfiltrate_everything"] = lambda **_: None

    with pytest.raises(ToolDispatchError, match="does not declare"):
        bind(catalog, handlers)


def test_the_handler_roster_matches_the_catalog_exactly(
    catalog: McpToolCatalogAdapter,
) -> None:
    """`HANDLER_NAMES` is documentation, so it is held to the catalog rather than trusted."""
    assert set(mcp_server.HANDLER_NAMES) == {spec.name for spec in catalog.list_tools()}


# --------------------------------------------------------------------------- #
# Packaging
# --------------------------------------------------------------------------- #
def _render(tmp_path: pathlib.Path) -> pathlib.Path:
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts"))
    import render_plugin

    render_plugin.main(["--dest", str(tmp_path / "plugin")])
    return tmp_path / "plugin"


def test_the_manifest_validates_against_the_vendored_specification_schema(
    tmp_path: pathlib.Path,
) -> None:
    """`jsonschema` is a hard dev dependency so this can never quietly skip into green."""
    from hex_service_kit.plugin import load_schema

    root = _render(tmp_path)
    manifest = json.loads((root / "plugin.json").read_text())

    jsonschema.validate(manifest, load_schema("plugin"))


def test_the_manifest_cannot_advertise_a_tool_the_catalog_does_not_declare(
    tmp_path: pathlib.Path, catalog: McpToolCatalogAdapter
) -> None:
    """Rendered from the declarations, so the manifest is not a second description to maintain."""
    root = _render(tmp_path)
    manifest = json.loads((root / "plugin.json").read_text())
    declared = {spec.name.replace("_", "-") for spec in catalog.list_tools()}

    assert set(manifest["keywords"]) == declared


def test_the_skills_folder_carries_the_vendored_agent_skills(tmp_path: pathlib.Path) -> None:
    """`skills/` means instructional SKILL.md documents, never the card's capabilities."""
    root = _render(tmp_path)
    skills = sorted(p.name for p in (root / "skills").iterdir() if p.is_dir())

    assert skills
    for skill in skills:
        assert (root / "skills" / skill / "SKILL.md").is_file(), skill
