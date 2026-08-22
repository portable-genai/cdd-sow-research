"""Unit tests for Mode 6's session token (adapters/oidc/session_token.py) and the
OidcSessionIdentityAdapter (adapters/oidc/session_identity.py) that verifies it.

Guarded by ``pytest.importorskip("jwt")``: these need the optional ``oidc`` extra
(``pyjwt[crypto]``), so the core `.[dev]`-only CI leg skips this module cleanly rather than
failing, proving the SDK-free import path stays intact (see test_oidc_auth_flow.py's
import-safety test for that proof).
"""

from __future__ import annotations

import pytest

jwt = pytest.importorskip("jwt")

from cdd_sow_research.adapters.oidc import session_identity, session_token  # noqa: E402
from cdd_sow_research.config import Container, IdentitySettings, Settings  # noqa: E402
from cdd_sow_research.domain.identity import IdentityError, RequestContext  # noqa: E402

_SIGNING_KEY_ENV = "TEST_CDD_SESSION_SIGNING_KEY"
_SIGNING_KEY = "unit-test-signing-key-at-least-32-bytes-long"


@pytest.fixture(autouse=True)
def _signing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_SIGNING_KEY_ENV, _SIGNING_KEY)


def _settings() -> Settings:
    base = Settings.load("config/settings.yaml")
    return Settings(
        **{
            **base.__dict__,
            "identity": IdentitySettings(
                mode="oidc-session",
                session_signing_key_env=_SIGNING_KEY_ENV,
                bindings=base.identity.bindings,
            ),
        }
    )


def _adapter() -> session_identity.OidcSessionIdentityAdapter:
    return session_identity.OidcSessionIdentityAdapter(_settings())


def _cookie_header(token: str) -> dict[str, str]:
    return {"cookie": f"{session_token.SESSION_COOKIE_NAME}={token}"}


# --------------------------------------------------------------------------- #
# session_token.mint / verify
# --------------------------------------------------------------------------- #
def test_mint_and_verify_round_trip() -> None:
    token = session_token.mint(
        {"sub": "demo.analyst@bank.example", "tenant": "demo-bank"},
        typ="session",
        signing_key_env=_SIGNING_KEY_ENV,
        ttl_seconds=3600,
    )
    claims = session_token.verify(token, typ="session", signing_key_env=_SIGNING_KEY_ENV)
    assert claims["sub"] == "demo.analyst@bank.example"
    assert claims["tenant"] == "demo-bank"


def test_type_confusion_is_rejected() -> None:
    """A txn token must never verify as a session token, even with a valid signature."""
    txn = session_token.mint(
        {"state": "x"}, typ="txn", signing_key_env=_SIGNING_KEY_ENV, ttl_seconds=600
    )
    with pytest.raises(IdentityError, match="expected a 'session' token"):
        session_token.verify(txn, typ="session", signing_key_env=_SIGNING_KEY_ENV)


def test_tampered_signature_is_rejected() -> None:
    token = session_token.mint(
        {"sub": "x"}, typ="session", signing_key_env=_SIGNING_KEY_ENV, ttl_seconds=3600
    )
    with pytest.raises(IdentityError):
        session_token.verify(token[:-4] + "abcd", typ="session", signing_key_env=_SIGNING_KEY_ENV)


def test_expired_token_is_rejected() -> None:
    token = session_token.mint(
        {"sub": "x"}, typ="session", signing_key_env=_SIGNING_KEY_ENV, ttl_seconds=-10
    )
    with pytest.raises(IdentityError):
        session_token.verify(token, typ="session", signing_key_env=_SIGNING_KEY_ENV)


def test_missing_signing_key_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_SIGNING_KEY_ENV, raising=False)
    with pytest.raises(IdentityError, match="not configured"):
        session_token.mint(
            {"sub": "x"}, typ="session", signing_key_env=_SIGNING_KEY_ENV, ttl_seconds=3600
        )


# --------------------------------------------------------------------------- #
# OidcSessionIdentityAdapter.resolve
# --------------------------------------------------------------------------- #
def test_resolve_valid_session_cookie() -> None:
    token = session_token.mint(
        {
            "sub": "demo.analyst@bank.example",
            "tenant": "demo-bank",
            "principals": ["group:cdd-analyst", "user:demo.analyst@bank.example"],
            "assurance": "mfa",
        },
        typ="session",
        signing_key_env=_SIGNING_KEY_ENV,
        ttl_seconds=3600,
    )
    principal = _adapter().resolve(RequestContext(headers=_cookie_header(token)))
    assert principal.subject == "demo.analyst@bank.example"
    assert principal.tenant == "demo-bank"
    assert principal.principals == ("group:cdd-analyst", "user:demo.analyst@bank.example")
    assert principal.assurance == "mfa"
    assert principal.source == "oidc-session"
    assert principal.actor == principal.subject


def test_resolve_no_cookie_header_raises() -> None:
    with pytest.raises(IdentityError):
        _adapter().resolve(RequestContext(headers={}))


def test_resolve_missing_session_cookie_name_raises() -> None:
    with pytest.raises(IdentityError):
        _adapter().resolve(RequestContext(headers={"cookie": "some_other_cookie=value"}))


def test_resolve_expired_session_cookie_raises() -> None:
    token = session_token.mint(
        {"sub": "x"}, typ="session", signing_key_env=_SIGNING_KEY_ENV, ttl_seconds=-10
    )
    with pytest.raises(IdentityError):
        _adapter().resolve(RequestContext(headers=_cookie_header(token)))


def test_resolve_txn_cookie_rejected_as_session() -> None:
    """The type-confusion defense must hold at the adapter, not just the token layer."""
    txn = session_token.mint(
        {"state": "x"}, typ="txn", signing_key_env=_SIGNING_KEY_ENV, ttl_seconds=600
    )
    with pytest.raises(IdentityError):
        _adapter().resolve(RequestContext(headers=_cookie_header(txn)))


def test_resolve_session_missing_subject_raises() -> None:
    token = session_token.mint(
        {"tenant": "demo-bank"},  # no 'sub'
        typ="session",
        signing_key_env=_SIGNING_KEY_ENV,
        ttl_seconds=3600,
    )
    with pytest.raises(IdentityError, match="missing 'sub'"):
        _adapter().resolve(RequestContext(headers=_cookie_header(token)))


# --------------------------------------------------------------------------- #
# Identity binding: oidc-session is independent of the runtime adapter profile.
# --------------------------------------------------------------------------- #
def test_oidc_session_profile_binds_the_right_adapter() -> None:
    settings = _settings()
    container = Container(settings)
    assert isinstance(container.identity, session_identity.OidcSessionIdentityAdapter)
