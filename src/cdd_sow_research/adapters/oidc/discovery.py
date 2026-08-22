"""OIDC Discovery: derive an issuer's endpoints from just its ``issuer`` URL.

Given only ``issuer`` (e.g. ``https://idp.example.com``), ``/auth/login`` and
``/auth/callback`` (``api/auth.py``) need the issuer's ``authorization_endpoint``,
``token_endpoint``, ``jwks_uri`` and (for logout) ``end_session_endpoint``. Fetching and
caching the standard discovery document (RFC OpenID Connect Discovery 1.0,
``{issuer}/.well-known/openid-configuration``) keeps per-tenant config to just an issuer +
client credentials instead of four hand-configured URLs per IdP.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from ...domain.identity import IdentityError

_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
_CACHE_TTL_SECONDS = 3600


@dataclass(frozen=True)
class DiscoveryDocument:
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    end_session_endpoint: str = ""


_cache: dict[str, tuple[float, DiscoveryDocument]] = {}


def discover(issuer: str) -> DiscoveryDocument:
    """Fetch (or return the cached) discovery document for ``issuer``.

    Fails closed: a fetch/parse failure, or a response missing any of the three required
    endpoints, raises :class:`IdentityError` rather than returning a partial document.
    """
    cached = _cache.get(issuer)
    if cached is not None and time.monotonic() - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]

    url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    try:
        response = httpx.get(url, timeout=_TIMEOUT)
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise IdentityError(f"OIDC discovery failed for issuer {issuer!r}: {exc}") from exc

    try:
        document = DiscoveryDocument(
            authorization_endpoint=data["authorization_endpoint"],
            token_endpoint=data["token_endpoint"],
            jwks_uri=data["jwks_uri"],
            end_session_endpoint=data.get("end_session_endpoint", ""),
        )
    except KeyError as exc:
        raise IdentityError(
            f"OIDC discovery document for issuer {issuer!r} is missing {exc}"
        ) from exc

    _cache[issuer] = (time.monotonic(), document)
    return document
