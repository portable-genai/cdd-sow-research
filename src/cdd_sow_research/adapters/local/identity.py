"""Local IdentityPort adapter: seeded dev personas, NO IdP / AD / LDAP.

The SDK-free ``local`` profile must run with zero authentication so demos and tests work fully
offline. This adapter resolves a :class:`Principal` from a small set of seeded personas, selected
by the ``X-Dev-Persona`` request header (the UI's persona picker), defaulting to the first persona
when none is supplied. It lets you exercise per-user authorization (different entitlement
principals and tenants, including a cross-tenant persona) without standing up any identity
provider. Bound ONLY under the local profile; secure mode uses the IAP adapter.

**Sourced from the shared ``hex-service-kit`` commons.** The persona-resolution
engine delegates to :class:`hex_service_kit.identity.LocalPersonaIdentityAdapter`; this module
keeps the CDD-specific persona SET (the entitlement groups and tenants this repo's authorization
logic reads) and the ``Settings``-based constructor the DI container binds.
"""

from __future__ import annotations

from hex_service_kit.identity import IdentityError, Principal, RequestContext
from hex_service_kit.identity import LocalPersonaIdentityAdapter as _PersonaEngine

from ...config import Settings
from ...ports.identity import CLIENT_ASSERTED

# Seeded dev personas. Ordered; the first entry is the default when no persona is selected. The
# persona id is the suffix of ``source`` after the colon (e.g. "analyst").
_PERSONAS: tuple[Principal, ...] = (
    Principal(
        subject="demo.analyst@bank.example",
        principals=("group:cdd-analyst", "group:risk"),
        tenant="demo-bank",
        assurance="local-demo",
        source="local-persona:analyst",
    ),
    Principal(
        subject="demo.approver@bank.example",
        principals=("group:cdd-analyst", "group:risk", "group:cdd-approver"),
        tenant="demo-bank",
        assurance="local-demo",
        source="local-persona:approver",
    ),
    Principal(
        subject="demo.auditor@bank.example",
        principals=("group:audit",),
        tenant="demo-bank",
        assurance="local-demo",
        source="local-persona:auditor",
    ),
    Principal(
        subject="user@other-tenant.example",
        principals=("group:cdd-analyst",),
        tenant="other-bank",
        assurance="local-demo",
        source="local-persona:other-tenant",
    ),
)


class LocalPersonaProfileError(IdentityError):
    """Raised when seeded dev personas would be served under a profile nobody chose."""


class LocalPersonaIdentityAdapter:
    """Resolve a Principal from a seeded dev persona (local profile only, no auth)."""

    #: The persona arrives on the X-Dev-Persona header the CALLER writes, so a caller
    #: chooses who it is. That is a picker, not authentication, and the exposure guard
    #: reads this declaration to keep the routes it serves off the LAN.
    end_user_auth = CLIENT_ASSERTED

    def __init__(self, settings: Settings) -> None:
        # These personas are an UNAUTHENTICATED grant of the analyst and approver
        # entitlements, so the adapter refuses to construct unless the no-auth posture was
        # actually chosen. Reading a missing CDD_PROFILE as "local" infers "local-persona",
        # which hands every caller the first seeded persona. It is an IdentityError subclass
        # so the API turns the refusal into a 401 rather than a 500.
        if not settings.identity_mode_explicit:
            raise LocalPersonaProfileError(
                "neither CDD_PROFILE nor CDD_IDENTITY_PROFILE is set, so the no-auth persona "
                "mode was inherited rather than chosen; seeded dev personas authenticate "
                "nobody and are refused. Set CDD_PROFILE=local deliberately for a dev or demo "
                "run, or CDD_PROFILE=gcp for a real deployment."
            )
        self._settings = settings
        self._engine = _PersonaEngine(_PERSONAS)

    def resolve(self, ctx: RequestContext) -> Principal:
        return self._engine.resolve(ctx)

    def personas(self) -> tuple[dict[str, str], ...]:
        """List the seeded personas for the local persona picker (id, subject, tenant)."""
        return self._engine.personas()
