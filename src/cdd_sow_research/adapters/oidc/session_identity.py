"""IdentityPort adapter for Mode 6 ("launch in new tab"): verify the agent's own session
cookie, minted by ``/auth/callback`` after an OIDC Authorization Code + PKCE login.

Deliberately distinct from the planned Mode 4 adapter that verifies an institution-issued
OAuth access token for the Doc1 resource (docs/embedding-implementation-plan.md Phase 3):
this adapter verifies a token the agent itself signed, so there is no external JWKS fetch
on the per-request hot path, just a fast local signature check. See
docs/embedding-and-identity.md Section 4.4 for the channel design and Section 13 for the
threat model.
"""

from __future__ import annotations

from http.cookies import SimpleCookie

from ...config import Settings
from ...domain.identity import IdentityError, Principal, RequestContext
from ...ports.identity import VERIFIED
from . import session_token


class OidcSessionIdentityAdapter:
    """Verify the agent's own signed session cookie and derive a Principal (Mode 6)."""

    #: Verifies a session token this agent itself signed after an OIDC login, so the
    #: cookie a caller presents cannot name a subject the agent did not mint.
    end_user_auth = VERIFIED

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def resolve(self, ctx: RequestContext) -> Principal:
        raw_cookie_header = ctx.header("cookie")
        if not raw_cookie_header:
            raise IdentityError("no session cookie present; sign in via /auth/login")
        cookie = SimpleCookie()
        cookie.load(raw_cookie_header)
        morsel = cookie.get(session_token.SESSION_COOKIE_NAME)
        if morsel is None:
            raise IdentityError(
                f"no {session_token.SESSION_COOKIE_NAME!r} cookie present; sign in via /auth/login"
            )
        claims = session_token.verify(
            morsel.value,
            typ="session",
            signing_key_env=self._settings.identity.session_signing_key_env,
            accepted_key_envs=self._settings.identity.session_accepted_key_envs,
        )
        subject = str(claims.get("sub") or "").strip()
        if not subject:
            raise IdentityError("session token missing 'sub' claim")
        tenant = str(claims.get("tenant") or "").strip()
        principals = tuple(claims.get("principals") or ())
        assurance = str(claims.get("assurance") or "").strip()
        return Principal(
            subject=subject,
            principals=principals,
            tenant=tenant,
            assurance=assurance,
            source="oidc-session",
        )
