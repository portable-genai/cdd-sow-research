"""Dedicated adverse-media sub-agent that isolates the built-in ``google_search`` tool.

The Gemini Enterprise Agent Platform allows **only one built-in tool per agent** (SPEC §3
gotcha). The root agent already carries our CDD ``FunctionTool`` wrappers, so public-web
adverse-media scanning via the Gemini API ``google_search`` tool must live in its own
sub-agent. The root agent reaches it as an ``AgentTool`` (an agent-as-tool), keeping the
built-in tool quarantined in this one place.

Adverse-media scanning is **secondary, cross-border** evidence and is toggled per
deployment via ``settings.grounding_enabled`` (SPEC §2). When disabled this module builds
no sub-agent at all, so no ``google_search`` traffic can leave the tenancy.

``google.adk`` is imported lazily inside the factory so this module imports without ADK
installed (SPEC §4).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..config import Settings

if TYPE_CHECKING:  # pragma: no cover - typing only
    from google.adk.agents import LlmAgent

ADVERSE_MEDIA_AGENT_NAME = "adverse_media_grounding"

_INSTRUCTION = (
    "You retrieve secondary, public-web adverse-media evidence about a subject for a "
    "customer due-diligence case. Use the google_search tool to find credible "
    "negative-news hits relevant to financial crime (fraud, corruption, sanctions, "
    "money laundering, terrorism). Return concise, quote-backed findings with their "
    "source titles and URLs. Never fabricate a citation, and treat web results as "
    "corroborating evidence only, not as a substitute for the case's KYC documents."
)


def build_adverse_media_agent(settings: Settings) -> LlmAgent | None:
    """Build the ``google_search``-only adverse-media sub-agent, or ``None`` if disabled.

    Gated on ``settings.grounding_enabled``. Uses the triage model
    (``settings.models.triage``) because the scan is a cheap, narrow lookup, and carries
    exactly one built-in tool (``google_search``). Imports ``google.adk`` lazily (SPEC §4).
    """
    if not settings.grounding_enabled:
        return None

    from google.adk.agents import LlmAgent
    from google.adk.tools import google_search

    return LlmAgent(
        name=ADVERSE_MEDIA_AGENT_NAME,
        model=settings.models.triage,
        description=(
            "Public-web adverse-media grounding via the Gemini API google_search tool; "
            "returns secondary, cross-border negative-news evidence for a subject."
        ),
        instruction=_INSTRUCTION,
        tools=[google_search],
    )
