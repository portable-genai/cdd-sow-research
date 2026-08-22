"""Mode 6 ID-token verification against a reviewed OIDC issuer and JWKS.

Consumed by the Mode 6 login flow when ``/auth/callback`` verifies the IdP's returned
``id_token``. The keyset is fetched via ``httpx`` so tests can mock it with ``respx``.
PyJWT (the optional ``oidc`` extra) is imported lazily so the SDK-free ``local`` and
``onprem`` profiles never need it installed.

The planned Mode 4 adapter must use a distinct OAuth access-token verifier with
``typ=at+jwt``, resource-audience, client, scope, tenant, and claim-mapping checks. It may
reuse low-level reviewed JWKS cache code after refactoring, but it must not call
``verify_id_token``. See docs/embedding-and-identity.md Section 6.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from ...domain.identity import IdentityError

_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
_CACHE_TTL_SECONDS = 3600
# Issuers come from the finite trusted_issuers list, so the cache stays tiny in practice;
# the cap is a backstop so a misconfigured multi-tenant deployment cannot grow it unbounded.
_MAX_CACHED_ISSUERS = 64

# jwks_uri -> (fetched_at, {kid: jwk_dict}). One fetch serves every token verified against
# that issuer until the cache expires or an unknown kid forces a refetch (rotation).
_cache: dict[str, tuple[float, dict[str, dict[str, Any]]]] = {}


def _fetch_keys(jwks_uri: str) -> dict[str, dict[str, Any]]:
    try:
        response = httpx.get(jwks_uri, timeout=_TIMEOUT)
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise IdentityError(f"JWKS fetch failed for {jwks_uri}: {exc}") from exc
    keys = {jwk["kid"]: jwk for jwk in data.get("keys", []) if "kid" in jwk}
    if jwks_uri not in _cache and len(_cache) >= _MAX_CACHED_ISSUERS:
        oldest = min(_cache, key=lambda uri: _cache[uri][0])
        del _cache[oldest]
    _cache[jwks_uri] = (time.monotonic(), keys)
    return keys


def _signing_jwk(jwks_uri: str, kid: str) -> dict[str, Any]:
    cached = _cache.get(jwks_uri)
    fresh = cached is not None and time.monotonic() - cached[0] < _CACHE_TTL_SECONDS
    if cached is not None and fresh and kid in cached[1]:
        return cached[1][kid]
    # Cache miss or expired: (re)fetch once. A genuinely unknown kid after a fresh fetch is
    # a hard failure, not a retry loop.
    keys = _fetch_keys(jwks_uri)
    jwk = keys.get(kid)
    if jwk is None:
        raise IdentityError(f"no key with kid={kid!r} in JWKS at {jwks_uri}")
    return jwk


def signing_jwk(jwks_uri: str, kid: str) -> dict[str, Any]:
    """Return one configured JWKS key with bounded caching and rotation refetch."""
    return _signing_jwk(jwks_uri, kid)


def verify_id_token(
    token: str,
    *,
    jwks_uri: str,
    issuer: str,
    audience: str,
    algorithms: tuple[str, ...] = ("RS256", "ES256"),
    leeway_seconds: int = 60,
) -> dict[str, Any]:
    """Verify ``token``'s signature, issuer, audience and expiry; return its claims.

    Fails closed: a JWKS-fetch failure, an unknown ``kid``, a signature mismatch, an
    algorithm outside ``algorithms`` (this is what rejects ``alg=none`` and blocks
    HS*-with-public-key confusion — the header's algorithm is never trusted, only what's in
    ``algorithms``), or any claim mismatch raises :class:`IdentityError`.
    """
    import jwt  # lazy: keeps local/onprem SDK-free (mirrors adapters/gcp/*)

    try:
        header = jwt.get_unverified_header(token)
    except Exception as exc:  # noqa: BLE001 - a malformed token must fail closed
        raise IdentityError(f"could not read token header: {exc}") from exc
    kid = header.get("kid")
    if not kid:
        raise IdentityError("token header missing 'kid'")

    jwk = _signing_jwk(jwks_uri, kid)
    try:
        signing_key = jwt.PyJWK(jwk).key
    except Exception as exc:  # noqa: BLE001 - a malformed JWK must fail closed
        raise IdentityError(f"invalid JWK for kid={kid!r}: {exc}") from exc

    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            signing_key,
            algorithms=list(algorithms),
            audience=audience,
            issuer=issuer,
            leeway=leeway_seconds,
            options={"require": ["exp", "iat"]},
        )
    except Exception as exc:  # noqa: BLE001 - any verification failure must become a 401
        raise IdentityError(f"token verification failed: {exc}") from exc
    return claims
