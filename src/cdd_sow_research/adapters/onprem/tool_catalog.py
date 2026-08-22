"""On-prem placeholder for ``ToolCatalogPort`` — the sovereign target.

One of the reversibility (P-02, P-12) migration placeholders: in the managed profile
this port binds to the MCP tool-catalog adapter; switching ``profile`` to ``onprem``
rebinds it here. The adapter constructs cleanly with **no external dependencies** and
structurally satisfies the same Protocol as the managed adapter, so the contract tests
prove interface parity. Porting the governed, least-privilege tool catalog to an
on-premise registry is *only* a matter of filling these bodies in: the domain is
unchanged.
"""

from __future__ import annotations

from ...config import Settings
from ...domain.models import ToolSpec

_MESSAGE = (
    "On-prem ToolCatalogPort adapter is a migration placeholder; implement against your "
    "on-premise platform. Core domain logic is unchanged."
)


class OnPremToolCatalogAdapter:
    """Placeholder tool-catalog adapter for the on-prem profile."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def list_tools(self) -> list[ToolSpec]:
        raise NotImplementedError(_MESSAGE)

    def get_tool(self, name: str) -> ToolSpec | None:
        raise NotImplementedError(_MESSAGE)
