"""IdentityPort - resolve a verified Principal from inbound transport context.

The hexagon boundary for authentication. The API layer hands the adapter a
:class:`RequestContext` (the request headers) and gets back a verified :class:`Principal`, or an
:class:`IdentityError`. The active profile picks the adapter: ``local`` resolves a seeded dev
persona (no IdP/AD/LDAP) so demos and tests run offline, ``gcp`` verifies the Identity-Aware-Proxy
signed assertion, and ``onprem`` is the placeholder for the client's own enterprise IdP.

**Sourced from the shared ``hex-service-kit`` commons:** the port Protocol is the same,
re-exported here so ``from cdd_sow_research.ports.identity import IdentityPort`` keeps working.
"""

from __future__ import annotations

from hex_service_kit.identity import IdentityPort

# --------------------------------------------------------------------------- #
# What an identity adapter DECLARES about the end-user authentication it provides.
#
# The exposure guard on the app object has one question to answer before it can decide
# anything: are this service's END-USER routes authenticated? Nothing else in the
# configuration answers it.
#
# * The RUNTIME PROFILE names a data-adapter family, not an authentication scheme, and this
#   repo separates the two on purpose: ``CDD_PROFILE`` and ``CDD_IDENTITY_PROFILE`` move
#   independently, so ``local`` can carry a verifying identity mode and a managed profile can
#   carry none.
# * The IDENTITY MODE is closer, but still a name rather than a claim: it is a key into
#   ``identity.bindings``, and a deployment may rebind any mode (the documented on-premises
#   path swaps the placeholder for the client's own IdP adapter) without the mode string
#   changing at all.
# * A SERVICE-TO-SERVICE secret authenticates a calling SERVICE. It authenticates no end
#   user, so its presence is not evidence that an end-user route is protected. Deriving the
#   guard from one would switch it OFF for the very routes it was protecting.
#
# The adapter bound to the identity port is the only thing that knows, so it says so here.
# --------------------------------------------------------------------------- #

#: The adapter verifies a server-side assertion; the client cannot assert who it is.
VERIFIED = "verified"
#: The adapter believes a header the client wrote. Useful offline, not authentication.
CLIENT_ASSERTED = "client-asserted"
#: The adapter resolves nobody: a placeholder for an identity provider not yet bound.
UNIMPLEMENTED = "unimplemented"

#: Every declaration this service understands. Anything else is read as CLIENT_ASSERTED.
END_USER_AUTH_KINDS: frozenset[str] = frozenset({VERIFIED, CLIENT_ASSERTED, UNIMPLEMENTED})

#: The class attribute an identity adapter sets to one of the values above. A CLASS attribute,
#: not an instance one, because the posture has to be readable WITHOUT constructing the
#: adapter: several of these adapters refuse to construct without their reviewed policy
#: present, and a posture that can only be computed by constructing something disappears
#: exactly when it matters most.
END_USER_AUTH_ATTR = "end_user_auth"


def declared_end_user_auth(adapter: object) -> str:
    """What ``adapter`` (a class or an instance) declares, defaulting to CLIENT_ASSERTED.

    An adapter that declares NOTHING is read as :data:`CLIENT_ASSERTED`, never
    :data:`VERIFIED`. Silence is not a claim to verify anything, and a guard that reads
    silence as "authenticated" switches itself off for every adapter somebody forgot to
    annotate, which is the fail-open shape this vocabulary exists to remove. An unrecognised
    value lands in the same place, so a typo cannot read as a verification claim.
    """
    declared = getattr(adapter, END_USER_AUTH_ATTR, None)
    if isinstance(declared, str) and declared in END_USER_AUTH_KINDS:
        return declared
    return CLIENT_ASSERTED


__all__ = [
    "CLIENT_ASSERTED",
    "END_USER_AUTH_ATTR",
    "END_USER_AUTH_KINDS",
    "UNIMPLEMENTED",
    "VERIFIED",
    "IdentityPort",
    "declared_end_user_auth",
]
