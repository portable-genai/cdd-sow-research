"""On-premises BrowserFlowStorePort placeholder."""

from __future__ import annotations

from ..disabled.browser_flow_store import DisabledBrowserFlowStoreAdapter


class OnPremBrowserFlowStoreAdapter(DisabledBrowserFlowStoreAdapter):
    """Fail fast until the client supplies its shared transactional store."""
