"""Stateless, session-bound CSRF protection for Mode 6 browser sessions.

The browser obtains a short-lived token from an authenticated, no-store GET endpoint
and keeps it only in memory. The token is bound to the session ``jti`` and the exact
unsafe method/path, so it cannot be replayed across actions or sessions. No second
cookie or server-side CSRF state exists.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from fastapi import HTTPException, Request, status

from ..adapters.oidc import session_token
from ..config import Settings
from ..envread import optional_setting
from .security import AuthenticatedContext, get_authenticated_context

CSRF_HEADER = "X-CSRF-Token"
_TOKEN_VERSION = 1
_TOKEN_TTL_SECONDS = 90
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# These two HTML form actions cannot attach the in-memory header used by the
# JavaScript transport. They remain protected by the same exact-origin browser
# provenance check here and by their route-owned, one-time browser-flow bindings:
# ``start`` consumes the opaque fragment ticket, while ``continue`` requires the
# callback transaction plus the consumed record and authenticated actor.
_SERVER_OWNED_FORM_PATHS = frozenset(
    {
        "/auth/citation/start",
        "/auth/citation/continue",
    }
)


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + ("=" * (-len(value) % 4)))


def _binding_key(settings: Settings, session_jti: str) -> bytes:
    secret = (optional_setting(settings.identity.session_signing_key_env) or "").encode()
    if not secret or not session_jti:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "CSRF protection requires a current session binding",
        )
    return hmac.new(secret, b"doc1-mode6-csrf\x00" + session_jti.encode(), hashlib.sha256).digest()


def _validate_target(method: str, path: str) -> tuple[str, str]:
    canonical_method = method.strip().upper()
    if canonical_method not in _UNSAFE_METHODS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "CSRF tokens are issued only for unsafe HTTP methods",
        )
    if (
        not path.startswith("/")
        or path.startswith("//")
        or "?" in path
        or "#" in path
        or "\\" in path
        or len(path) > 512
    ):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "CSRF target path is invalid")
    return canonical_method, path


def mint_csrf_token(
    settings: Settings,
    context: AuthenticatedContext,
    *,
    method: str,
    path: str,
    now: int | None = None,
) -> str:
    """Mint one short-lived token bound to this session and exact action."""
    canonical_method, canonical_path = _validate_target(method, path)
    issued_at = int(time.time()) if now is None else now
    payload = {
        "v": _TOKEN_VERSION,
        "nonce": secrets.token_urlsafe(24),
        "iat": issued_at,
        "exp": issued_at + _TOKEN_TTL_SECONDS,
        "method": canonical_method,
        "path": canonical_path,
    }
    encoded = _b64encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(
        _binding_key(settings, context.evidence.session_jti),
        encoded.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{encoded}.{_b64encode(signature)}"


def verify_csrf_token(
    token: str,
    settings: Settings,
    context: AuthenticatedContext,
    *,
    method: str,
    path: str,
    now: int | None = None,
) -> None:
    """Verify signature, lifetime, session binding, and exact action binding."""
    canonical_method, canonical_path = _validate_target(method, path)
    try:
        encoded, supplied_signature = token.split(".", 1)
        expected_signature = hmac.new(
            _binding_key(settings, context.evidence.session_jti),
            encoded.encode("ascii"),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(_b64decode(supplied_signature), expected_signature):
            raise ValueError("signature mismatch")
        payload: dict[str, Any] = json.loads(_b64decode(encoded))
        current = int(time.time()) if now is None else now
        if (
            payload.get("v") != _TOKEN_VERSION
            or not isinstance(payload.get("nonce"), str)
            or len(payload["nonce"]) < 22
            or not isinstance(payload.get("iat"), int)
            or not isinstance(payload.get("exp"), int)
            or payload["iat"] > current + 5
            or payload["exp"] < current
            or payload["exp"] - payload["iat"] != _TOKEN_TTL_SECONDS
            or payload.get("method") != canonical_method
            or payload.get("path") != canonical_path
        ):
            raise ValueError("claim mismatch")
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF token is invalid") from exc


def enforce_cookie_csrf(request: Request, settings: Settings) -> None:
    """Protect every unsafe request carrying a Mode 6 first-party session cookie."""
    if (
        request.method.upper() not in _UNSAFE_METHODS
        or settings.identity_mode != "oidc-session"
        or session_token.SESSION_COOKIE_NAME not in request.cookies
    ):
        return
    expected_origin = settings.channel.public_origin.rstrip("/")
    if (
        not expected_origin
        or request.headers.get("origin", "") != expected_origin
        or request.headers.get("sec-fetch-site", "") != "same-origin"
    ):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "unsafe session request failed the same-origin policy",
        )
    if request.url.path in _SERVER_OWNED_FORM_PATHS:
        return
    context = get_authenticated_context(request)
    supplied = request.headers.get(CSRF_HEADER, "")
    if not supplied:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF token is required")
    verify_csrf_token(
        supplied,
        settings,
        context,
        method=request.method,
        path=request.url.path,
    )


def private_no_store_headers() -> dict[str, str]:
    return {
        "Cache-Control": "private, no-store",
        "Pragma": "no-cache",
        "Referrer-Policy": "no-referrer",
    }
