"""Identity value objects for server-side, verified principals.

**Sourced from the shared ``hex-service-kit`` commons.** ``Principal``,
``RequestContext``, ``IdentityError`` and ``ANONYMOUS`` are the same value objects, re-exported
here so existing imports (``from cdd_sow_research.domain.identity import Principal``) keep working
unchanged. The agent never trusts a client-asserted ``actor`` or ACL: a :class:`Principal` is
resolved server-side by an :class:`~cdd_sow_research.ports.identity.IdentityPort` adapter from the
inbound transport context, and becomes the audit actor plus the entitlement principals fed into
governed retrieval. Pure stdlib: nothing here imports a web framework or a cloud SDK.
"""

from __future__ import annotations

from hex_service_kit.identity import (
    ANONYMOUS,
    IdentityError,
    Principal,
    RequestContext,
)

__all__ = ["ANONYMOUS", "IdentityError", "Principal", "RequestContext"]
