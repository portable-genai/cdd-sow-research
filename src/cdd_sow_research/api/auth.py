"""Mode 6 ("launch in new tab"): OIDC Authorization Code + PKCE redirect login.

Three unauthenticated routes — ``/auth/login``, ``/auth/callback``, ``/auth/logout`` — that
let a browser tab (opened via true top-level navigation, never an iframe: see
docs/embedding-and-identity.md Section 4.4) sign the user in against ANY OIDC-compliant IdP
and receive the agent's own first-party session cookie. None of these depend on
:data:`~cdd_sow_research.api.security.CurrentPrincipal`: they are, by definition, how a caller
becomes authenticated in the first place.

Design constraints mirrored from ``api/app.py``:

* **Import-safe.** ``jwt`` (the optional ``oidc`` extra) is imported lazily inside
  ``adapters/oidc/*`` helper functions this module calls, never at this module's top level,
  so importing ``api.app`` still works with no PyJWT installed — the routes below simply
  fail closed (503) if no trusted issuer is configured or the extra is missing.
* **Never logs a secret or a token.** Client secrets, PKCE verifiers, and issued tokens are
  never written to a log line.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import urllib.parse
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ..adapters.oidc import discovery, jwks_verify, session_token
from ..config import IdentitySettings, IssuerSettings, Settings
from ..domain.identity import IdentityError, Principal
from ..envread import optional_setting
from . import deps
from .csrf import mint_csrf_token, private_no_store_headers
from .security import CurrentAuthenticatedContext, canonical_actor

router = APIRouter(prefix="/auth", tags=["auth"])

_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
_TXN_TTL_SECONDS = 600  # 10 minutes: long enough to complete an interactive IdP login
_TOKEN_AUTH_METHODS = frozenset({"client_secret_basic", "client_secret_post"})


def _require_oidc_session(settings: Settings) -> None:
    """Hide the login surface unless the exact identity mode enables it."""
    try:
        mode = settings.identity_mode
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "OIDC session login is disabled") from exc
    if mode != "oidc-session":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "OIDC session login is disabled")


def _select_issuer(identity: IdentitySettings, tenant: str | None) -> IssuerSettings:
    """Resolve which configured issuer drives the login (Mode 6 is single-issuer-per-
    deployment for MVP; ``?tenant=`` selects among several when more than one is configured)."""
    if not identity.trusted_issuers:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "OIDC login is not configured: no trusted issuer in identity.trusted_issuers",
        )
    if tenant:
        for candidate in identity.trusted_issuers:
            if candidate.tenant == tenant:
                return candidate
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown tenant {tenant!r}")
    return identity.trusted_issuers[0]


def _client_secret(issuer: IssuerSettings) -> str:
    secret = optional_setting(issuer.client_secret_env) if issuer.client_secret_env else None
    if not issuer.client_id or not secret:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"OIDC client_id/client_secret not configured for issuer {issuer.issuer!r}",
        )
    return secret


def _validated_discovery(issuer: IssuerSettings) -> discovery.DiscoveryDocument:
    """Fetch discovery and constrain it to the reviewed issuer and endpoint policy."""
    discovery_url = issuer.issuer.rstrip("/") + "/.well-known/openid-configuration"
    try:
        response = httpx.get(discovery_url, timeout=_TIMEOUT)
        response.raise_for_status()
        payload = response.json()
        if payload.get("issuer") != issuer.issuer:
            raise ValueError("discovery issuer does not match configured issuer")
        document = discovery.DiscoveryDocument(
            authorization_endpoint=payload["authorization_endpoint"],
            token_endpoint=payload["token_endpoint"],
            jwks_uri=payload["jwks_uri"],
            end_session_endpoint=payload.get("end_session_endpoint", ""),
        )
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"OIDC discovery policy failed for issuer {issuer.issuer!r}: {exc}",
        ) from exc

    issuer_url = urllib.parse.urlsplit(issuer.issuer)
    allowed_hosts = {issuer_url.hostname or "", *issuer.allowed_endpoint_hosts}
    for label, endpoint in (
        ("authorization_endpoint", document.authorization_endpoint),
        ("token_endpoint", document.token_endpoint),
        ("jwks_uri", document.jwks_uri),
        ("end_session_endpoint", document.end_session_endpoint),
    ):
        if not endpoint:
            continue
        parsed = urllib.parse.urlsplit(endpoint)
        loopback_http = (
            parsed.scheme == "http"
            and issuer_url.scheme == "http"
            and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        )
        if parsed.scheme != "https" and not loopback_http:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                f"OIDC {label} violates the HTTPS endpoint policy",
            )
        if (parsed.hostname or "") not in allowed_hosts:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                f"OIDC {label} host is not in the reviewed issuer policy",
            )
    if issuer.token_endpoint_auth_method not in _TOKEN_AUTH_METHODS:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "OIDC token endpoint authentication method is not enabled",
        )
    supported_auth = payload.get("token_endpoint_auth_methods_supported")
    if supported_auth is not None and issuer.token_endpoint_auth_method not in supported_auth:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "configured token endpoint authentication method is absent from discovery",
        )
    return document


def _safe_return_to(return_to: str, allowed_hosts: tuple[str, ...]) -> str:
    """Same-origin-only unless the target host is explicitly allowlisted.

    Blocks absolute/protocol-relative URLs (``https://evil.example``, ``//evil.example``)
    and the classic backslash bypass (``/\\evil.example``, which some browsers normalise
    into a protocol-relative URL even though this parser would not).
    """
    if not return_to or not return_to.startswith("/"):
        return "/"
    if return_to.startswith("//") or return_to.startswith("/\\"):
        return "/"
    parsed = urllib.parse.urlsplit(return_to)
    if parsed.netloc and parsed.netloc not in allowed_hosts:
        return "/"
    return return_to


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def oidc_authorization_redirect(
    settings: Settings,
    issuer: IssuerSettings,
    document: discovery.DiscoveryDocument,
    *,
    return_to: str,
    additional_transaction_claims: dict[str, Any] | None = None,
) -> RedirectResponse:
    """Mint one signed OIDC transaction and redirect to the reviewed authorize URL."""
    code_verifier = secrets.token_urlsafe(64)
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    claims: dict[str, Any] = {
        "state": state,
        "nonce": nonce,
        "code_verifier": code_verifier,
        "return_to": return_to,
        "issuer": issuer.issuer,
    }
    additions = additional_transaction_claims or {}
    reserved = set(claims) & set(additions)
    if reserved:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "OIDC transaction extension attempted to replace a reserved claim",
        )
    claims.update(additions)
    txn = session_token.mint(
        claims,
        typ="txn",
        signing_key_env=settings.identity.session_signing_key_env,
        ttl_seconds=_TXN_TTL_SECONDS,
    )
    authorize_url = f"{document.authorization_endpoint}?" + urlencode(
        {
            "response_type": "code",
            "client_id": issuer.client_id,
            "redirect_uri": settings.public_callback_uri,
            "scope": "openid email profile",
            "state": state,
            "nonce": nonce,
            "code_challenge": _pkce_challenge(code_verifier),
            "code_challenge_method": "S256",
        }
    )
    response = RedirectResponse(authorize_url, status_code=status.HTTP_302_FOUND)
    response.set_cookie(
        session_token.TXN_COOKIE_NAME,
        txn,
        max_age=_TXN_TTL_SECONDS,
        httponly=True,
        secure=True,
        samesite="lax",
        path=f"{settings.channel.public_mount_path}/auth",
    )
    return response


@router.get("/login")
def auth_login(request: Request, return_to: str = "", tenant: str = "") -> RedirectResponse:
    """Start an OIDC Authorization Code + PKCE login; redirects to the IdP's login page."""
    settings = deps.get_settings()
    _require_oidc_session(settings)
    issuer = _select_issuer(settings.identity, tenant or None)
    _client_secret(issuer)  # fail fast (503) before sending the user anywhere
    document = _validated_discovery(issuer)

    safe_return_to = _safe_return_to(return_to, settings.identity.allowed_return_to_hosts)
    return oidc_authorization_redirect(
        settings,
        issuer,
        document,
        return_to=safe_return_to,
    )


@router.get("/callback", name="auth_callback", response_model=None)
def auth_callback(
    request: Request, code: str = "", state: str = ""
) -> RedirectResponse | HTMLResponse:
    """Complete the login: exchange ``code``, verify the ID token, mint the session cookie."""
    settings = deps.get_settings()
    _require_oidc_session(settings)
    raw_txn = request.cookies.get(session_token.TXN_COOKIE_NAME, "")
    if not raw_txn:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "missing login transaction cookie")
    try:
        txn_claims = session_token.verify(
            raw_txn,
            typ="txn",
            signing_key_env=settings.identity.session_signing_key_env,
            accepted_key_envs=settings.identity.session_accepted_key_envs,
        )
    except IdentityError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    if not state or not secrets.compare_digest(state, str(txn_claims.get("state", ""))):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "state mismatch")
    if not code:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "missing authorization code")

    issuer = next(
        (i for i in settings.identity.trusted_issuers if i.issuer == txn_claims.get("issuer")),
        None,
    )
    if issuer is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "unknown issuer in transaction")
    document = _validated_discovery(issuer)

    token_response = _exchange_code(
        document.token_endpoint,
        code=code,
        code_verifier=str(txn_claims["code_verifier"]),
        client_id=issuer.client_id,
        client_secret=_client_secret(issuer),
        redirect_uri=settings.public_callback_uri,
        auth_method=issuer.token_endpoint_auth_method,
    )
    id_token = token_response.get("id_token", "")
    if not id_token:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "IdP token response missing id_token")

    try:
        claims = jwks_verify.verify_id_token(
            id_token,
            jwks_uri=document.jwks_uri,
            issuer=issuer.issuer,
            audience=issuer.client_id,  # ID-token audience is the RP's client_id (OIDC spec)
            algorithms=issuer.algorithms,
        )
    except IdentityError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc

    # Nonce binding (OIDC §3.1.3.7): the id_token must echo the nonce we generated for
    # THIS login, defeating id-token replay across logins.
    expected_nonce = str(txn_claims.get("nonce", ""))
    if not expected_nonce or not secrets.compare_digest(
        str(claims.get("nonce", "")), expected_nonce
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "id_token nonce mismatch")

    source_subject = str(claims.get("sub") or "").strip()
    if not source_subject:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "id_token missing sub claim")
    audience = claims.get("aud")
    if isinstance(audience, list) and len(audience) > 1 and claims.get("azp") != issuer.client_id:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "multi-audience id_token missing matching azp",
        )
    reviewed_tenant = issuer.tenant.strip()
    claimed_tenant = str(claims.get(issuer.tenant_claim) or "").strip()
    if not reviewed_tenant:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "id_token did not resolve a policy-mapped tenant",
        )
    if claimed_tenant and not secrets.compare_digest(claimed_tenant, reviewed_tenant):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "id_token tenant does not match the reviewed issuer mapping",
        )
    # The reviewed issuer mapping is authoritative. The claim can corroborate it but
    # can never select or override a tenant.
    tenant = reviewed_tenant
    subject = canonical_actor(issuer.issuer, source_subject)
    display_email = str(claims.get("email") or "").strip()
    principals = [f"group:{g}" for g in claims.get(issuer.groups_claim) or []]
    principals.append(f"user:{subject}")
    token_scopes = tuple(scope for scope in str(token_response.get("scope") or "").split() if scope)

    session = session_token.mint(
        {
            "sub": subject,
            "tenant": tenant,
            "principals": principals,
            "assurance": str(claims.get("acr", "")),
            "issuer": issuer.issuer,
            "source_sub": source_subject,
            "authorized_client": issuer.client_id,
            "scopes": token_scopes,
            "display_email": display_email,
            "jti": secrets.token_urlsafe(24),
        },
        typ="session",
        signing_key_env=settings.identity.session_signing_key_env,
        ttl_seconds=settings.identity.session_ttl_seconds,
    )

    from .citation_continuation import (
        citation_confirmation_response,
        complete_citation_callback,
    )

    citation = complete_citation_callback(
        settings,
        txn_claims,
        Principal(
            subject=subject,
            principals=tuple(principals),
            tenant=tenant,
            assurance=str(claims.get("acr", "")),
            source="oidc-session",
        ),
    )
    if citation is not None:
        return citation_confirmation_response(session, settings)

    return_to = _safe_return_to(
        str(txn_claims.get("return_to", "/")), settings.identity.allowed_return_to_hosts
    )
    response = RedirectResponse(return_to, status_code=status.HTTP_302_FOUND)
    response.set_cookie(
        session_token.SESSION_COOKIE_NAME,
        session,
        max_age=settings.identity.session_ttl_seconds,
        httponly=True,
        secure=True,
        samesite="strict",
        path="/",
    )
    # A __Secure--prefixed cookie is only deleted by a Set-Cookie that also carries
    # Secure; without it a conformant browser rejects the deletion and the txn cookie
    # lingers. Mirror the attributes used to set it.
    response.delete_cookie(
        session_token.TXN_COOKIE_NAME,
        path=f"{settings.channel.public_mount_path}/auth",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    return response


def _exchange_code(
    token_endpoint: str,
    *,
    code: str,
    code_verifier: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    auth_method: str,
) -> dict[str, Any]:
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": code_verifier,
        "client_id": client_id,
        "redirect_uri": redirect_uri,
    }
    auth: tuple[str, str] | None = None
    if auth_method == "client_secret_basic":
        auth = (client_id, client_secret)
    elif auth_method == "client_secret_post":
        data["client_secret"] = client_secret
    else:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "OIDC token endpoint authentication method is not enabled",
        )
    try:
        response = httpx.post(
            token_endpoint,
            data=data,
            auth=auth,
            timeout=_TIMEOUT,
        )
        response.raise_for_status()
        response_data: dict[str, Any] = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"token exchange failed: {exc}") from exc
    return response_data


@router.post("/logout")
def auth_logout(request: Request) -> RedirectResponse:
    """Clear the session cookie; optionally propagate to the issuer's own logout."""
    settings = deps.get_settings()
    _require_oidc_session(settings)
    redirect_url = "/"
    raw_session = request.cookies.get(session_token.SESSION_COOKIE_NAME, "")
    if raw_session:
        redirect_url = _try_end_session_redirect(settings, raw_session) or redirect_url

    response = RedirectResponse(redirect_url, status_code=status.HTTP_302_FOUND)
    # A __Host--prefixed cookie is only deleted by a Set-Cookie that carries Secure,
    # Path=/ and no Domain; without Secure a conformant browser rejects the deletion and
    # the session cookie survives logout until the JWT expires. Mirror the set attributes.
    response.delete_cookie(
        session_token.SESSION_COOKIE_NAME, path="/", secure=True, httponly=True, samesite="strict"
    )
    return response


@router.get("/csrf")
def csrf_bootstrap(
    context: CurrentAuthenticatedContext,
    method: str,
    path: str,
) -> JSONResponse:
    """Issue a session/action-bound CSRF token for browser-memory storage only."""
    settings = deps.get_settings()
    _require_oidc_session(settings)
    return JSONResponse(
        {"csrf_token": mint_csrf_token(settings, context, method=method, path=path)},
        headers=private_no_store_headers(),
    )


def _try_end_session_redirect(settings: Settings, raw_session: str) -> str | None:
    """Best-effort: if the session's issuer wants logout propagated, return its end-session
    URL. Any failure here (expired/invalid session, discovery failure) must not block
    logout, so every error is swallowed and the caller falls back to a local-only logout."""
    try:
        claims = session_token.verify(
            raw_session, typ="session", signing_key_env=settings.identity.session_signing_key_env
        )
        issuer = next(
            (i for i in settings.identity.trusted_issuers if i.issuer == claims.get("issuer")),
            None,
        )
        if issuer is None or not issuer.end_session_logout:
            return None
        document = _validated_discovery(issuer)
        if not document.end_session_endpoint:
            return None
        return document.end_session_endpoint
    except Exception:  # noqa: BLE001 - logout must never fail because of this best-effort step
        return None
