"""The declared MCP schema and the served handler must agree about the payload.

``test_mcp_surface_is_served_and_packaged.py`` already pins the NAMES in both directions, and
``hex_service_kit.mcpserve.bind`` refuses to start on a name mismatch. Neither says anything
about the *payload*: a tool can be declared, bound, served and still describe arguments its
handler never reads, or read arguments the caller was never told to send. That is the defect the
portfolio backlog recorded twice about one tool in ``loan-document-intelligence``:
``cross_validate`` was narrowed once from an unresolvable ``extract_ids`` to a REQUIRED
``documents`` array, and the callable ignored ``documents`` entirely. A peer agent was still
being told to send a payload for a run the tool does not perform.

Both of those narrowings were done by READING, and reading is what missed it the second time.
So this file compares the two sides mechanically. The handlers take ``**arguments``, so there is
no signature to check a schema against; what there is, and what decides the outcome of a call,
is which keys the handler actually reads out of that bag. That is read from the AST rather than
by calling anything, because the handlers are closures over a live container and this has to
hold under the offline gate.

Three conventions are honoured rather than worked around. A handler that declares its bag
``**_`` reads nothing on purpose, and its schema must therefore declare no properties. Keys
reached through a helper taking ``arguments`` count as read by every handler that calls it,
transitively -- several tools in this fleet share one input builder. And ``actor`` is never
askable: it is the verified identity the server resolves, so accepting it from a caller would
be a forged audit subject.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from cdd_sow_research.adapters.gcp.mcp_tool_catalog import McpToolCatalogAdapter
from cdd_sow_research.config import Settings
from cdd_sow_research.mcp import server as mcp_server

CONFIG_PATH = "config/settings.yaml"

#: Never askable. The server resolves the acting identity; a client-asserted one is a forgery.
#: ``settings`` is the dependency-injection seam that no caller passes.
_SERVER_OWNED = frozenset({"actor", "settings"})


@pytest.fixture
def catalog() -> McpToolCatalogAdapter:
    return McpToolCatalogAdapter(Settings.load(CONFIG_PATH))


def _module_ast() -> ast.Module:
    """The served module's own source. Parsed, never imported for this purpose."""
    return ast.parse(Path(inspect.getsourcefile(mcp_server) or "").read_text())


def _keys_read(node: ast.AST, bag: str) -> set[str]:
    """Keys taken out of ``bag`` as ``bag["k"]`` or ``bag.get("k", ...)``."""
    keys: set[str] = set()
    for sub in ast.walk(node):
        if (
            isinstance(sub, ast.Subscript)
            and isinstance(sub.value, ast.Name)
            and sub.value.id == bag
            and isinstance(sub.slice, ast.Constant)
            and isinstance(sub.slice.value, str)
        ):
            keys.add(sub.slice.value)
        elif (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Attribute)
            and sub.func.attr == "get"
            and isinstance(sub.func.value, ast.Name)
            and sub.func.value.id == bag
            and sub.args
            and isinstance(sub.args[0], ast.Constant)
            and isinstance(sub.args[0].value, str)
        ):
            keys.add(sub.args[0].value)
    return keys


def _calls(node: ast.AST) -> set[str]:
    return {
        sub.func.id
        for sub in ast.walk(node)
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
    }


def _handler_reads() -> dict[str, set[str]]:
    """Tool name -> every argument key its handler can read, helpers included."""
    tree = _module_ast()
    factory = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "build_handlers"
    )
    # A helper is any function in the module taking a parameter literally named `arguments`,
    # at module level or nested: this fleet reaches its input keys through both.
    helpers = {
        n.name: n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and any(a.arg == "arguments" for a in n.args.args)
    }
    reads: dict[str, set[str]] = {}
    for fn in (n for n in factory.body if isinstance(n, ast.FunctionDef) and n.args.kwarg):
        bag = fn.args.kwarg.arg
        keys = _keys_read(fn, bag)
        seen: set[str] = set()
        frontier = _calls(fn) & set(helpers)
        while frontier:
            helper = frontier.pop()
            if helper in seen:
                continue
            seen.add(helper)
            keys |= _keys_read(helpers[helper], "arguments")
            frontier |= (_calls(helpers[helper]) & set(helpers)) - seen
        reads[fn.name] = keys
    return reads


def test_the_ast_walk_finds_the_handlers_it_is_supposed_to_check() -> None:
    """Guard against a vacuous suite: a rename here would silently check nothing."""
    reads = _handler_reads()
    assert set(reads) == set(mcp_server.HANDLER_NAMES), (
        "the AST walk and the declared handler roster disagree, so every assertion below is "
        "checking a different set of tools than the one that gets served"
    )


def test_every_declared_argument_is_one_the_handler_actually_reads(
    catalog: McpToolCatalogAdapter,
) -> None:
    """A declared argument the handler ignores is a payload asked for and thrown away."""
    reads = _handler_reads()
    specs = catalog.list_tools()
    assert specs, "an empty catalog would make this assertion vacuous"
    for spec in specs:
        declared = set(spec.input_schema.get("properties", {}))
        unread = declared - reads[spec.name]
        assert not unread, (
            f"{spec.name} declares {sorted(unread)}, which its handler never reads: a peer "
            "agent is being told to send a payload the callable silently ignores"
        )


def test_every_argument_the_handler_reads_is_one_the_caller_was_told_about(
    catalog: McpToolCatalogAdapter,
) -> None:
    """The mirror defect, and the one a shared input builder causes.

    When several tools construct their input through one helper, the narrow declarations stop
    mentioning keys the helper still reads. The caller cannot know to send them, so the handler
    silently substitutes a default and the result depends on a value nobody chose.
    """
    reads = _handler_reads()
    for spec in catalog.list_tools():
        declared = set(spec.input_schema.get("properties", {}))
        undeclared = reads[spec.name] - declared
        assert not undeclared, (
            f"{spec.name}'s handler reads {sorted(undeclared)}, which the schema does not "
            "declare: the caller cannot supply it and the handler defaults it silently"
        )


def test_no_declared_schema_leaks_a_server_owned_argument(
    catalog: McpToolCatalogAdapter,
) -> None:
    for spec in catalog.list_tools():
        leaked = _SERVER_OWNED & set(spec.input_schema.get("properties", {}))
        assert not leaked, f"{spec.name} makes {sorted(leaked)} askable by the caller"


def test_a_required_argument_is_never_one_the_handler_ignores(
    catalog: McpToolCatalogAdapter,
) -> None:
    """The sharpest form of the defect: REQUIRED and ignored.

    Optional-and-ignored is a wrong declaration. Required-and-ignored is a refused call for a
    value that changes nothing, which is why it is asserted separately.
    """
    reads = _handler_reads()
    for spec in catalog.list_tools():
        required = set(spec.input_schema.get("required", []))
        ignored = required - reads[spec.name]
        assert not ignored, f"{spec.name} REQUIRES {sorted(ignored)} and then ignores it"


def test_an_input_less_tool_declares_no_arguments(catalog: McpToolCatalogAdapter) -> None:
    """``**_`` is this fleet's way of saying a tool reads nothing. The schema must say so too."""
    reads = _handler_reads()
    for spec in catalog.list_tools():
        if reads[spec.name]:
            continue
        assert not spec.input_schema.get("properties"), (
            f"{spec.name}'s handler reads no argument at all, so its declared properties "
            f"{sorted(spec.input_schema.get('properties', {}))} are all unreachable"
        )
