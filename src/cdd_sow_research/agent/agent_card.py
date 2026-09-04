"""A2A AgentCard for the B1 CDD + Source-of-Wealth Agent (A3 Registry & Governance).

This builds the agent's discovery card (the same minimal A2A shape the
``agent-registry`` service stores and serves, SPEC §6). It is published at
``/.well-known/agent-card.json``; :func:`agent_card_document` returns the JSON-safe body
the API layer serves there.

The card advertises the CDD skills B1 produces (assess_cdd, build_source_of_wealth,
scan_adverse_media, resolve_ownership, resolve_ubo_graph, run_perpetual_kyc,
list_perpetual_kyc_queue), mirroring the ADK FunctionTools so a peer agent or the
registry sees one consistent capability surface.

``version`` is the CONTRACT version a consumer pins: the ``resolve_ubo_graph`` output
shape is frozen against it (docs/ubo-graph-contract.md), so a field may be added
without a bump but never renamed, retyped or removed without one.

This module is pure (domain models only) and imports without ADK or any Google Cloud SDK
installed (SPEC §4).
"""

from __future__ import annotations

from typing import Any

from ..config import Settings
from ..domain.models import AgentCard, AgentSkill

SKILLS: tuple[AgentSkill, ...] = (
    AgentSkill(
        id="assess_cdd",
        name="CDD dossier assessment",
        description=(
            "Build a full cited CDD dossier for a subject and its KYC documents: a "
            "source-of-wealth narrative, a risk rating, adverse-media findings, and a "
            "beneficial-ownership / UBO summary. Always flagged for human review (P-06)."
        ),
    ),
    AgentSkill(
        id="build_source_of_wealth",
        name="Source-of-wealth narrative",
        description=(
            "Synthesise a cited source-of-wealth narrative broken into wealth sources "
            "(employment, business ownership, inheritance, investments, asset sale), each "
            "corroborated by document, registry and media citations."
        ),
    ),
    AgentSkill(
        id="scan_adverse_media",
        name="Adverse-media scan",
        description=(
            "Scan public-web negative news for a subject and return categorised, severity "
            "ranked adverse-media findings with citations."
        ),
    ),
    AgentSkill(
        id="resolve_ownership",
        name="Beneficial-ownership resolution",
        description=(
            "Resolve the corporate-ownership / UBO picture for an entity from corporate "
            "registry sources, including PEP-flagged beneficial owners."
        ),
    ),
    AgentSkill(
        id="resolve_ubo_graph",
        name="Cross-jurisdiction UBO graph",
        description=(
            "Walk an entity's corporate ownership structure one cited registry hop at a "
            "time and return the layered graph, the natural persons whose EFFECTIVE "
            "ownership (the product of the shareholdings along each path) reaches the "
            "bank's threshold, the control basis (effective majority, voting majority, "
            "board majority, contractual, or the senior-managing-official fallback), and "
            "deterministic nominee/shell indicators with an opacity score. Cycles and "
            "cross-holdings terminate; every percentage is pure code, never the model. "
            "Always flagged for human review and routed to human-review-console."
        ),
    ),
    AgentSkill(
        id="run_perpetual_kyc",
        name="Perpetual-KYC re-score",
        description=(
            "Run one perpetual-KYC cycle: diff the current sanctions, adverse-media and "
            "corporate-registry picture against the stored baseline, re-score the "
            "relationship deterministically over bank-owned policy weights, and queue an "
            "explainable review item. Always flagged for human review and routed to "
            "human-review-console."
        ),
    ),
    AgentSkill(
        id="list_perpetual_kyc_queue",
        name="Perpetual-KYC review queue",
        description=(
            "List the caller's tenant-scoped perpetual-KYC review queue, most urgent "
            "first, with the signals that moved, the score arithmetic and the citations "
            "behind each queued item."
        ),
    ),
)

_DESCRIPTION = (
    "Grounded research agent for a private bank's financial-crime team. Turns a "
    "customer's KYC pack, corporate registries and adverse media into a cited CDD "
    "dossier (source-of-wealth narrative, risk rating, adverse-media findings, UBO "
    "summary), with a full audit trail. Built ports-and-adapters on the Gemini "
    "Enterprise Agent Platform; the governed RAG store is the A2 Enterprise KB."
)


def build_agent_card(settings: Settings) -> AgentCard:
    """Construct the A2A :class:`AgentCard` for this agent."""
    return AgentCard(
        name="cdd-sow-research",
        description=_DESCRIPTION,
        url=_resolve_url(settings),
        version="0.1.0",
        skills=SKILLS,
        provider="cdd-sow-research",
    )


def agent_card_document(settings: Settings) -> dict[str, Any]:
    """Return the JSON-safe body to serve at ``/.well-known/agent-card.json``."""
    from ..domain.serialization import to_jsonable

    return to_jsonable(build_agent_card(settings))


def _resolve_url(settings: Settings) -> str:
    """Best-effort public URL for the card in the configured deployment region."""
    resource = settings.agent_engine.resource_name
    if resource:
        return f"https://aiplatform.googleapis.com/v1/{resource}"
    return "https://cdd-sow-research.doc.internal/a2a"
