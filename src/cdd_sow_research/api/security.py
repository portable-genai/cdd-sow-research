"""Request authentication boundary for every browser identity mode.

The domain-facing ``IdentityPort`` remains stable. This API layer calls it exactly once,
then returns the verified ``Principal`` together with sanitized identity evidence. Route
code never reaches into a concrete adapter or re-verifies a credential through a side
channel.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from http.cookies import SimpleCookie
from typing import Annotated, Any, Protocol, runtime_checkable

from fastapi import Depends, HTTPException, Request, status
from hex_service_kit.federation import (
    IAP_ASSERTION_HEADER,
    IAP_ISSUER,
    IAP_KEYS_URL,
    PORTAL_ASSERTION_HEADER,
)

from ..adapters.oidc import session_token
from ..adapters.oidc.access_token_identity import OAuthAccessTokenAuthenticationAdapter
from ..adapters.oidc.configured_embed_identity import (
    ConfiguredEmbeddedGrantAuthenticationAdapter,
)
from ..config import Settings
from ..domain.identity import IdentityError, Principal, RequestContext
from ..envread import optional_setting
from ..ports.identity import EndUserAuthUnavailableError, IdentityPort
from . import deps

_CORRELATION = re.compile(r"^[A-Za-z0-9._:/-]{1,128}$")
_ACTOR_PREFIX = "issub:"

# The four transport facts are REBOUND from the commons, not re-declared here. This module and
# ``adapters/gcp/iap_identity.py`` each kept their own copies until 2026-08-26: the same
# strings written twice inside one repository, with nothing able to notice a divergence,
# because a literal always agrees with itself.
#
# ``PORTAL_ASSERTION_HEADER`` is the same assertion forwarded by a same-origin embedding host
# under a name Google's serverless frontend does not reserve and therefore does not strip. Read
# as a FALLBACK, never as an alternative trust path: the assertion it yields is verified exactly
# like the standard one, so a caller gains nothing by choosing the header. What it solves is
# transport, and the commons module owns the full reasoning.
_IAP_ISSUER = IAP_ISSUER
_IAP_ASSERTION_HEADER = IAP_ASSERTION_HEADER
_PORTAL_ASSERTION_HEADER = PORTAL_ASSERTION_HEADER
_IAP_KEYS_URL = IAP_KEYS_URL


def canonical_actor(issuer: str, source_subject: str) -> str:
    """Deterministically encode the immutable ``(iss, sub)`` pair as the audit actor."""
    pair = json.dumps([issuer, source_subject], separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(pair).decode("ascii").rstrip("=")
    return f"{_ACTOR_PREFIX}{encoded}"


def decode_canonical_actor(actor: str) -> tuple[str, str] | None:
    if not actor.startswith(_ACTOR_PREFIX):
        return None
    raw = actor.removeprefix(_ACTOR_PREFIX)
    try:
        padding = "=" * (-len(raw) % 4)
        value = json.loads(base64.urlsafe_b64decode(raw + padding))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(value, list)
        or len(value) != 2
        or not all(isinstance(item, str) and item for item in value)
    ):
        return None
    return value[0], value[1]


@dataclass(frozen=True, slots=True)
class IdentityEvidence:
    """Sanitized facts carried into authorization/audit, never a raw credential."""

    issuer: str
    source_subject: str
    token_type: str
    authorized_client: str = ""
    effective_scopes: tuple[str, ...] = ()
    installation: str = ""
    assurance: str = ""
    correlation: str = ""
    display_email: str = ""
    # Internal-only binding for stateless Mode 6 CSRF. It is never serialized into an
    # API response or audit event.
    session_jti: str = ""


@dataclass(frozen=True, slots=True)
class AuthenticatedIdentity:
    principal: Principal
    evidence: IdentityEvidence


@dataclass(frozen=True, slots=True)
class AuthenticatedContext:
    principal: Principal
    evidence: IdentityEvidence


@runtime_checkable
class AuthenticationPort(Protocol):
    """API-layer authentication contract, deliberately outside the domain port count."""

    def authenticate(self, ctx: RequestContext, *, correlation: str = "") -> AuthenticatedIdentity:
        """Verify one request and return its principal plus safe evidence."""
        ...


class IdentityPortAuthenticationAdapter:
    """Compatibility bridge from the stable domain IdentityPort to richer API evidence."""

    def __init__(self, identity: IdentityPort, settings: Settings) -> None:
        self._identity = identity
        self._settings = settings

    def authenticate(self, ctx: RequestContext, *, correlation: str = "") -> AuthenticatedIdentity:
        principal = self._identity.resolve(ctx)
        mode = self._settings.identity_mode
        decoded = decode_canonical_actor(principal.subject)
        if decoded is not None:
            issuer, source_subject = decoded
        elif mode == "iap":
            issuer, source_subject = _IAP_ISSUER, principal.subject
        elif mode == "local-persona":
            issuer, source_subject = "local://persona", principal.subject
        else:
            issuer = (
                self._settings.identity.trusted_issuers[0].issuer
                if (self._settings.identity.trusted_issuers)
                else mode
            )
            source_subject = principal.subject

        if mode != "local-persona" and not principal.tenant.strip():
            raise IdentityError(f"{mode} identity did not resolve a policy-mapped tenant")

        evidence = IdentityEvidence(
            issuer=issuer,
            source_subject=source_subject,
            token_type={
                "local-persona": "local-persona",
                "iap": "iap-assertion",
                "oidc-session": "session",
                "oauth-access-token": "at+jwt",
                "embedded-grant": "embedded-grant",
                "onprem": "onprem",
            }[mode],
            assurance=principal.assurance,
            correlation=correlation,
        )
        return AuthenticatedIdentity(principal=principal, evidence=evidence)


class OidcSessionAuthenticationAdapter:
    """Verify one Mode 6 session and retain its exact upstream provenance."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def authenticate(self, ctx: RequestContext, *, correlation: str = "") -> AuthenticatedIdentity:
        raw_cookie_header = ctx.header("cookie")
        if not raw_cookie_header:
            raise IdentityError("no session cookie present; sign in via /auth/login")
        cookie = SimpleCookie()
        cookie.load(raw_cookie_header)
        morsel = cookie.get(session_token.SESSION_COOKIE_NAME)
        if morsel is None:
            raise IdentityError("no Doc1 session cookie present; sign in via /auth/login")
        claims = session_token.verify(
            morsel.value,
            typ="session",
            signing_key_env=self._settings.identity.session_signing_key_env,
            accepted_key_envs=self._settings.identity.session_accepted_key_envs,
        )
        issuer = str(claims.get("issuer") or "").strip()
        source_subject = str(claims.get("source_sub") or "").strip()
        subject = str(claims.get("sub") or "").strip()
        tenant = str(claims.get("tenant") or "").strip()
        if not issuer or not source_subject or not subject:
            raise IdentityError("session is missing issuer-qualified subject provenance")
        if subject != canonical_actor(issuer, source_subject):
            raise IdentityError("session actor does not match its issuer-qualified subject")
        if not tenant:
            raise IdentityError("session did not resolve a policy-mapped tenant")
        principal = Principal(
            subject=subject,
            principals=tuple(claims.get("principals") or ()),
            tenant=tenant,
            assurance=str(claims.get("assurance") or ""),
            source="oidc-session",
        )
        evidence = IdentityEvidence(
            issuer=issuer,
            source_subject=source_subject,
            token_type="session",
            authorized_client=str(claims.get("authorized_client") or ""),
            effective_scopes=tuple(str(scope) for scope in claims.get("scopes") or ()),
            assurance=principal.assurance,
            correlation=correlation,
            display_email=str(claims.get("display_email") or ""),
            session_jti=str(claims.get("jti") or ""),
        )
        return AuthenticatedIdentity(principal=principal, evidence=evidence)


class IapAuthenticationAdapter:
    """Verify the IAP assertion once and preserve its immutable issuer/sub claims."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._audience = optional_setting("CDD_IAP_AUDIENCE") or ""

    def _groups_for(self, claims: Mapping[str, object]) -> tuple[str, ...]:
        """The reviewed group principals a VERIFIED identity domain holds here.

        Without this the IAP path granted ``user:<subject>`` alone, which satisfies no
        case-access role, so every signed-in analyst was refused every case they named. The
        groups are the same strings the entitlement rules already recognise; naming a domain here
        states who holds them, and grants nothing that was not already grantable.
        """

        hosted_domain = str(claims.get("hd") or "").strip().lower()
        email = str(claims.get("email") or "").strip().lower()
        domain = hosted_domain or email.rpartition("@")[2]
        return tuple(self._settings.identity.iap_groups_by_domain.get(domain, ()))

    def _tenant_for(self, claims: Mapping[str, object]) -> str:
        """Resolve the reviewed tenant for a VERIFIED assertion, or the empty string.

        ``hd`` is a Google hosted domain, not a tenant id, and it is absent entirely on a
        machine identity. The reviewed map is what turns the one into the other; with no map
        configured this returns ``hd`` exactly as before, so an existing deployment is unchanged.
        """

        hosted_domain = str(claims.get("hd") or "").strip().lower()
        # A service account presents no hosted domain, so its email domain is the closest thing
        # to one; naming that domain in the map is how a deployment admits a machine caller.
        email = str(claims.get("email") or "").strip().lower()
        domain = hosted_domain or email.rpartition("@")[2]
        mapping = self._settings.identity.iap_tenant_by_domain
        if not mapping:
            return hosted_domain
        return str(mapping.get(domain, "")).strip()

    def authenticate(self, ctx: RequestContext, *, correlation: str = "") -> AuthenticatedIdentity:
        assertion = ctx.header(_IAP_ASSERTION_HEADER) or ctx.header(_PORTAL_ASSERTION_HEADER)
        if not assertion:
            raise IdentityError("missing IAP assertion header; request did not pass through IAP")
        if not self._audience:
            raise IdentityError("CDD_IAP_AUDIENCE is not configured; cannot verify IAP assertion")
        claims = self._verify(assertion)
        issuer = str(claims.get("iss") or "").strip()
        source_subject = str(claims.get("sub") or "").strip()
        tenant = self._tenant_for(claims)
        if issuer != _IAP_ISSUER or not source_subject:
            raise IdentityError("IAP assertion is missing the exact issuer/sub identity")
        if not tenant:
            raise IdentityError("IAP assertion did not resolve a policy-mapped tenant")
        subject = canonical_actor(issuer, source_subject)
        principal = Principal(
            subject=subject,
            principals=(f"user:{subject}", *self._groups_for(claims)),
            tenant=tenant,
            assurance="iap",
            source="gcp-iap",
        )
        evidence = IdentityEvidence(
            issuer=issuer,
            source_subject=source_subject,
            token_type="iap-assertion",
            authorized_client=str(claims.get("azp") or ""),
            effective_scopes=tuple(str(claims.get("scope") or "").split()),
            assurance="iap",
            correlation=correlation,
            display_email=str(claims.get("email") or ""),
        )
        return AuthenticatedIdentity(principal=principal, evidence=evidence)

    def _verify(self, assertion: str) -> dict[str, Any]:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token

        try:
            return dict(
                id_token.verify_token(
                    assertion,
                    google_requests.Request(),
                    audience=self._audience,
                    certs_url=_IAP_KEYS_URL,
                )
            )
        except Exception as exc:  # noqa: BLE001 - every verifier failure becomes a 401
            raise IdentityError(f"IAP assertion verification failed: {exc}") from exc


class OAuthAccessTokenApiAuthenticationAdapter:
    """Normalize the Mode 4 verifier result into the shared request context."""

    def __init__(self, verifier: OAuthAccessTokenAuthenticationAdapter) -> None:
        self._verifier = verifier

    def authenticate(self, ctx: RequestContext, *, correlation: str = "") -> AuthenticatedIdentity:
        verified = self._verifier.authenticate(ctx)
        evidence = IdentityEvidence(
            issuer=verified.issuer,
            source_subject=verified.source_subject,
            token_type="at+jwt",
            authorized_client=verified.authorized_client,
            effective_scopes=verified.effective_scopes,
            installation=verified.installation_id,
            assurance=verified.principal.assurance,
            correlation=verified.correlation or correlation,
        )
        return AuthenticatedIdentity(principal=verified.principal, evidence=evidence)


class EmbeddedGrantApiAuthenticationAdapter:
    """Normalize one exact Doc1 embedded token without accepting other token types."""

    def __init__(self, verifier: ConfiguredEmbeddedGrantAuthenticationAdapter) -> None:
        self._verifier = verifier

    def authenticate(self, ctx: RequestContext, *, correlation: str = "") -> AuthenticatedIdentity:
        verified = self._verifier.authenticate(ctx)
        token = verified.verified_token
        evidence = IdentityEvidence(
            issuer=token.source_issuer,
            source_subject=token.source_subject,
            token_type="embedded-grant",
            authorized_client=token.client_id,
            effective_scopes=token.scopes,
            installation=token.installation_id,
            assurance=verified.principal.assurance,
            correlation=token.correlation or correlation,
        )
        return AuthenticatedIdentity(principal=verified.principal, evidence=evidence)


def get_authentication_port() -> AuthenticationPort:
    container = deps.get_container()
    if container.settings.identity_mode == "oidc-session":
        return OidcSessionAuthenticationAdapter(container.settings)
    if container.settings.identity_mode == "iap":
        return IapAuthenticationAdapter(container.settings)
    if container.settings.identity_mode == "oauth-access-token":
        verifier = container.identity
        if not isinstance(verifier, OAuthAccessTokenAuthenticationAdapter):
            raise RuntimeError("oauth-access-token identity binding is not the Mode 4 verifier")
        return OAuthAccessTokenApiAuthenticationAdapter(verifier)
    if container.settings.identity_mode == "embedded-grant":
        verifier = container.identity
        if not isinstance(verifier, ConfiguredEmbeddedGrantAuthenticationAdapter):
            raise RuntimeError("embedded-grant identity binding is not the exact verifier")
        return EmbeddedGrantApiAuthenticationAdapter(verifier)
    return IdentityPortAuthenticationAdapter(container.identity, container.settings)


def get_authenticated_context(request: Request) -> AuthenticatedContext:
    """Verify the request once and map all identity failures to a non-secret 401."""
    existing = getattr(request.state, "authenticated_context", None)
    if isinstance(existing, AuthenticatedContext):
        return existing
    headers = {key.lower(): value for key, value in request.headers.items()}
    headers[":method"] = request.method
    headers[":path"] = request.url.path
    candidate = headers.get("x-request-id", "")
    correlation = candidate if _CORRELATION.fullmatch(candidate) else ""
    try:
        authenticated = get_authentication_port().authenticate(
            RequestContext(headers=headers), correlation=correlation
        )
    # The deployment's own failure is answered BEFORE the caller's, and the order is
    # load-bearing: EndUserAuthUnavailableError is an IdentityError subclass, so the reverse
    # order silently swallows it and answers the 401 the whole split exists to avoid. Its
    # message reaches the operator, because no credential would have helped them; an ordinary
    # refusal still answers a bare 401 that tells an unauthenticated caller nothing they could
    # use to forge the next attempt.
    except EndUserAuthUnavailableError as exc:
        raise HTTPException(exc.http_status, str(exc)) from exc
    except (IdentityError, NotImplementedError) as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    context = AuthenticatedContext(
        principal=authenticated.principal,
        evidence=authenticated.evidence,
    )
    request.state.authenticated_context = context
    return context


def get_principal(context: Annotated[AuthenticatedContext, Depends(get_authenticated_context)]):
    return context.principal


_DOMAIN_SCOPES = frozenset(
    {
        "cdd.read",
        "cdd.write",
        "documents.read",
        "documents.write",
    }
)
# These identity modes authorize domain scopes through their existing server-owned ACL
# policy rather than OAuth scope claims. Keeping the grants explicit prevents a newly
# added identity mode from silently inheriting full API access.
_NON_OAUTH_SCOPE_POLICY: dict[str, frozenset[str]] = {
    "local-persona": _DOMAIN_SCOPES,
    "iap": _DOMAIN_SCOPES,
    "oidc-session": _DOMAIN_SCOPES,
    "onprem": _DOMAIN_SCOPES,
}


def require_scopes(*required_scopes: str):
    """Return a dependency enforcing OAuth scopes for Modes 4/5 on one route."""
    required = frozenset(required_scopes)
    unknown = required - _DOMAIN_SCOPES
    if not required or unknown:
        raise ValueError(f"route scope policy is invalid: {sorted(unknown)}")

    def dependency(
        context: Annotated[AuthenticatedContext, Depends(get_authenticated_context)],
    ) -> Principal:
        mode = deps.get_settings().identity_mode
        if mode in {"oauth-access-token", "embedded-grant"}:
            granted = frozenset(context.evidence.effective_scopes)
        else:
            granted = _NON_OAUTH_SCOPE_POLICY.get(mode, frozenset())
        missing = required - granted
        if missing:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"verified identity lacks required scope: {', '.join(sorted(missing))}",
            )
        return context.principal

    return dependency


CurrentAuthenticatedContext = Annotated[AuthenticatedContext, Depends(get_authenticated_context)]
CurrentPrincipal = Annotated[Principal, Depends(get_principal)]
CddReadPrincipal = Annotated[Principal, Depends(require_scopes("cdd.read"))]
CddWritePrincipal = Annotated[Principal, Depends(require_scopes("cdd.write"))]
DocumentsReadPrincipal = Annotated[Principal, Depends(require_scopes("documents.read"))]
DocumentsWritePrincipal = Annotated[Principal, Depends(require_scopes("documents.write"))]
# A complete case bundle is both halves of the case at once, so it carries both scopes.
# Splitting them would let a token holding only one scope move the other half.
BundleExportPrincipal = Annotated[Principal, Depends(require_scopes("cdd.read", "documents.read"))]
BundleImportPrincipal = Annotated[Principal, Depends(require_scopes("cdd.read", "documents.write"))]
CitationReadPrincipal = Annotated[
    Principal,
    Depends(require_scopes("cdd.read", "documents.read")),
]
