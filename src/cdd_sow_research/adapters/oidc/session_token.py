"""The agent's own signed tokens for Mode 6 ("launch in new tab"): the session cookie and
the short-lived login-transaction (PKCE/state) cookie.

Both are HMAC-SHA256 with the same shared secret (no external party ever needs the key, so
an asymmetric keypair buys nothing here) — but a ``typ`` claim distinguishes them so a
short-lived transaction token can never be replayed as a long-lived session token or vice
versa, even though they share a signing key. The signing key is never stored in
``config/settings.yaml`` — only the NAME of the env var holding it
(:attr:`~cdd_sow_research.config.IdentitySettings.session_signing_key_env`) is configured; the
value is read from the environment at mint/verify time and never logged.

Minted/verified by ``api/auth.py`` (transaction tokens, and minting the session token at
``/auth/callback``) and by
:class:`~cdd_sow_research.adapters.oidc.session_identity.OidcSessionIdentityAdapter` (verifying
the session token per request).
"""

from __future__ import annotations

import hashlib
import time
from typing import Any, Literal

from ...domain.identity import IdentityError
from ...envread import optional_setting

_ALGORITHM = "HS256"

TokenType = Literal["session", "txn"]

# Cookie names shared between api/auth.py (sets them) and OidcSessionIdentityAdapter
# (reads SESSION_COOKIE_NAME). Defined once here so the two never drift. The __Host- /
# __Secure- prefixes are browser-enforced hardening: __Host- guarantees the session
# cookie is Secure, Path=/ and host-scoped (no Domain, so a sibling subdomain cannot
# set it); __Secure- guarantees the /auth-scoped txn cookie is Secure.
SESSION_COOKIE_NAME = "__Host-cdd_session"
TXN_COOKIE_NAME = "__Secure-cdd_txn"


def _kid(key: str) -> str:
    """A short, non-secret key id derived from the key, published in the token header."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]


def _signing_key(env_var: str) -> str:
    key = optional_setting(env_var) or ""
    if not key:
        raise IdentityError(f"{env_var} is not configured; cannot mint/verify a token")
    return key


def _accepted_keys(signing_key_env: str, accepted_key_envs: tuple[str, ...]) -> list[str]:
    """Every key a token may be verified against: the current key plus rotation keys.

    Enables zero-downtime key rotation: point ``signing_key_env`` at the NEW key and list
    the previous env var(s) in ``accepted_key_envs``; tokens minted under the old key keep
    verifying until they expire, and only then is the old key retired.
    """
    keys = [optional_setting(env) or "" for env in (signing_key_env, *accepted_key_envs)]
    live = [k for k in keys if k]
    if not live:
        raise IdentityError(f"{signing_key_env} is not configured; cannot verify a token")
    return live


def mint(claims: dict[str, Any], *, typ: TokenType, signing_key_env: str, ttl_seconds: int) -> str:
    """Sign ``claims`` (plus ``typ``/``iat``/``exp``) into one of the agent's own tokens.

    The header carries a ``kid`` derived from the signing key so a verifier can tell which
    rotation key minted the token (and so a key change is observable in the token itself).
    """
    import jwt  # lazy: keeps local/onprem SDK-free (mirrors adapters/gcp/*)

    now = int(time.time())
    payload = {**claims, "typ": typ, "iat": now, "exp": now + ttl_seconds}
    key = _signing_key(signing_key_env)
    return jwt.encode(payload, key, algorithm=_ALGORITHM, headers={"kid": _kid(key)})


def verify(
    token: str,
    *,
    typ: TokenType,
    signing_key_env: str,
    accepted_key_envs: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Verify signature, expiry, and that ``typ`` matches; return the token's claims.

    Fails closed: a missing signing key, a signature that matches none of the accepted
    rotation keys, an expired token, or a ``typ`` mismatch (a transaction token presented
    as a session token, or vice versa) raises :class:`IdentityError`.
    """
    import jwt  # lazy: keeps local/onprem SDK-free (mirrors adapters/gcp/*)

    last_exc: Exception | None = None
    claims: dict[str, Any] | None = None
    for key in _accepted_keys(signing_key_env, accepted_key_envs):
        try:
            claims = jwt.decode(
                token, key, algorithms=[_ALGORITHM], options={"require": ["exp", "iat"]}
            )
            break
        except Exception as exc:  # noqa: BLE001 - try the next rotation key
            last_exc = exc
    if claims is None:
        raise IdentityError(f"token verification failed: {last_exc}")
    if claims.get("typ") != typ:
        raise IdentityError(f"expected a {typ!r} token, got {claims.get('typ')!r}")
    return claims
