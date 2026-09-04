"""Serve the governed tool catalog cdd-sow-research already declares, over MCP 2026-07-28.

The catalog declared four governed tools and served none of them: there was no MCP server
process anywhere in the fleet. This supplies the callables that answer the existing catalog and
declares nothing new. `hex_service_kit.mcpserve.bind` refuses a mismatch in either direction at
start-up, so a tool the service advertises and cannot perform does not start, and neither does a
handler for a tool nobody governed.

**Identity is the reason this module is careful.** Every service here takes a verified `actor`,
and several take `principals` and a `tenant` that decide what evidence is admitted and which
partition a case is written to. MCP stdio verifies no end user at all. So the caller identity is
supplied by the composition root and recorded as a SERVICE caller, and `principals` is left
EMPTY rather than filled with something plausible: entitlement filtering is fail-closed here, so
an empty principal sees untagged public evidence and nothing else. Manufacturing entitlements to
make a tool call return more would be the one change this module must never make.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from hex_service_kit import mcpserve

from ..api import deps
from ..domain.models import CaseInput, Subject, SubjectType

#: The tools this module answers, kept as data so a test can hold it against the catalog
#: without starting a server or importing the MCP SDK.
HANDLER_NAMES: tuple[str, ...] = (
    "assess_cdd",
    "build_source_of_wealth",
    "scan_adverse_media",
    "resolve_ownership",
)


def _subject(name: str, *, kind: str = "individual", jurisdiction: str = "") -> Subject:
    try:
        subject_type = SubjectType(kind)
    except ValueError:
        subject_type = SubjectType.INDIVIDUAL
    return Subject(id=f"mcp:{name}", name=name, type=subject_type, jurisdiction=jurisdiction)


def build_handlers(actor: str) -> dict[str, mcpserve.Handler]:
    """Bind each declared tool to the domain service that already performs it.

    ``actor`` is the audited caller, passed in rather than derived here because this transport
    verifies no end user. Nothing below widens the caller's scope: no ``principals`` are
    supplied, so retrieval stays on its fail-closed path.
    """
    container = deps.get_container()

    def assess_cdd(**arguments: Any) -> Any:
        subject = _subject(
            str(arguments.get("subject_name", "")),
            kind=str(arguments.get("subject_type", "individual")),
        )
        return deps.get_cdd_service().assess(CaseInput(subject=subject), actor=actor)

    def build_source_of_wealth(**arguments: Any) -> Any:
        """Return the dossier's source-of-wealth narrative.

        The standalone SoW path is a multi-step case workflow (open, add evidence, analyse) and
        this tool takes only a name, so answering it with the assessment's own SoW section is
        what the service actually does for a bare subject. It is not a cheaper path than the
        assessment and is not presented as one.
        """
        subject = _subject(str(arguments.get("subject_name", "")))
        return deps.get_cdd_service().assess(CaseInput(subject=subject), actor=actor).sow

    def scan_adverse_media(**arguments: Any) -> Any:
        service = deps.build_adverse_media_service(container)
        return service.scan(_subject(str(arguments.get("subject_name", ""))), actor)

    def resolve_ownership(**arguments: Any) -> Any:
        subject = _subject(
            str(arguments.get("entity_name", "")),
            kind="entity",
            jurisdiction=str(arguments.get("jurisdiction", "")),
        )
        return deps.build_ubo_graph_service(container).resolve(
            subject, actor=actor, as_of=date.today()
        )

    return {
        "assess_cdd": assess_cdd,
        "build_source_of_wealth": build_source_of_wealth,
        "scan_adverse_media": scan_adverse_media,
        "resolve_ownership": resolve_ownership,
    }


def build_server(actor: str, *, with_audit_tools: bool = True) -> Any:
    """Build the MCP server for cdd-sow-research's catalog, refusing on any catalog/handler
    mismatch.

    ``with_audit_tools`` adds the kit's two READ-ONLY evidence tools, so a client that can reach
    this service can also verify and carry out its trail. Read-only is enforced in the kit:
    appending to the trail is something a service does as it works, never something a caller
    asks for.
    """
    container = deps.get_container()
    return mcpserve.build_server(
        name="cdd-sow-research",
        version=str(getattr(container.settings, "version", "") or "0.0.1"),
        catalog=container.tool_catalog,
        handlers=build_handlers(actor),
        audit_store=container.audit if with_audit_tools else None,
    )
