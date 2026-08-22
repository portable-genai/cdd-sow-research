"""Regression tests for Mode 6 CSRF and Modes 4/5 route-scope enforcement."""

from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from cdd_sow_research.adapters.oidc import session_token
from cdd_sow_research.api import csrf, deps
from cdd_sow_research.api.security import (
    AuthenticatedContext,
    IdentityEvidence,
    require_scopes,
)
from cdd_sow_research.config import ChannelSettings, Settings
from cdd_sow_research.domain.identity import Principal

_SIGNING_KEY_ENV = "TEST_MODE6_CSRF_SIGNING_KEY"
_ORIGIN = "https://agent.test.example"


def _settings(mode: str) -> Settings:
    base = Settings.load("config/settings.yaml")
    return replace(
        base,
        identity=replace(
            base.identity,
            mode=mode,
            session_signing_key_env=_SIGNING_KEY_ENV,
        ),
        channel=ChannelSettings(mode="standalone", public_origin=_ORIGIN),
    )


def _context(*, scopes: tuple[str, ...] = (), jti: str = "session-one") -> AuthenticatedContext:
    principal = Principal(
        subject="actor-1",
        principals=("user:actor-1",),
        tenant="demo-bank",
        assurance="mfa",
        source="test",
    )
    return AuthenticatedContext(
        principal=principal,
        evidence=IdentityEvidence(
            issuer="https://idp.test.example",
            source_subject="subject-1",
            token_type="session",
            effective_scopes=scopes,
            session_jti=jti,
        ),
    )


def _request(
    *,
    path: str,
    origin: str = _ORIGIN,
    fetch_site: str = "same-origin",
    token: str = "",
) -> Request:
    headers = [
        (b"cookie", f"{session_token.SESSION_COOKIE_NAME}=opaque".encode()),
        (b"origin", origin.encode()),
        (b"sec-fetch-site", fetch_site.encode()),
    ]
    if token:
        headers.append((b"x-csrf-token", token.encode()))
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 1234),
            "server": ("agent.test.example", 443),
        }
    )


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_SIGNING_KEY_ENV, "csrf-signing-key-at-least-32-bytes-long")


def test_mode6_csrf_bootstrap_token_is_session_and_action_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings("oidc-session")
    first = _context(jti="session-one")
    token = csrf.mint_csrf_token(
        settings,
        first,
        method="POST",
        path="/v1/cdd",
        now=1_000,
    )
    csrf.verify_csrf_token(
        token,
        settings,
        first,
        method="POST",
        path="/v1/cdd",
        now=1_001,
    )
    with pytest.raises(HTTPException, match="CSRF token is invalid"):
        csrf.verify_csrf_token(
            token,
            settings,
            first,
            method="POST",
            path="/auth/logout",
            now=1_001,
        )
    with pytest.raises(HTTPException, match="CSRF token is invalid"):
        csrf.verify_csrf_token(
            token,
            settings,
            _context(jti="session-two"),
            method="POST",
            path="/v1/cdd",
            now=1_001,
        )


@pytest.mark.parametrize(
    ("origin", "fetch_site"),
    [
        ("https://sibling.test.example", "same-site"),
        (_ORIGIN, ""),
        ("", "same-origin"),
    ],
)
def test_mode6_csrf_rejects_sibling_wrong_or_missing_browser_provenance(
    monkeypatch: pytest.MonkeyPatch,
    origin: str,
    fetch_site: str,
) -> None:
    settings = _settings("oidc-session")
    monkeypatch.setattr(csrf, "get_authenticated_context", lambda _request: _context())
    request = _request(path="/auth/logout", origin=origin, fetch_site=fetch_site)

    with pytest.raises(HTTPException, match="same-origin policy"):
        csrf.enforce_cookie_csrf(request, settings)


def test_mode6_csrf_rejects_missing_or_wrong_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings("oidc-session")
    monkeypatch.setattr(csrf, "get_authenticated_context", lambda _request: _context())
    with pytest.raises(HTTPException, match="CSRF token is required"):
        csrf.enforce_cookie_csrf(_request(path="/auth/logout"), settings)
    with pytest.raises(HTTPException, match="CSRF token is invalid"):
        csrf.enforce_cookie_csrf(
            _request(path="/auth/logout", token="wrong.token"),
            settings,
        )


@pytest.mark.parametrize(
    "path",
    ["/auth/citation/start", "/auth/citation/continue"],
)
def test_mode6_server_owned_forms_use_exact_origin_flow_exemption(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    settings = _settings("oidc-session")

    def unexpected_context(_request: Request) -> AuthenticatedContext:
        raise AssertionError("form exemption must not require the JavaScript CSRF header")

    monkeypatch.setattr(csrf, "get_authenticated_context", unexpected_context)
    csrf.enforce_cookie_csrf(_request(path=path), settings)


@pytest.mark.parametrize(
    "path",
    [
        "/auth/citation/start/",
        "/auth/citation/continue-extra",
        "/agent/auth/citation/start",
    ],
)
def test_mode6_server_owned_form_exemption_rejects_nearby_routes(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    monkeypatch.setattr(csrf, "get_authenticated_context", lambda _request: _context())
    with pytest.raises(HTTPException, match="CSRF token is required"):
        csrf.enforce_cookie_csrf(_request(path=path), _settings("oidc-session"))


@pytest.mark.parametrize(
    ("origin", "fetch_site"),
    [
        ("https://sibling.test.example", "same-site"),
        (_ORIGIN, ""),
    ],
)
def test_mode6_server_owned_forms_still_require_exact_browser_provenance(
    origin: str,
    fetch_site: str,
) -> None:
    with pytest.raises(HTTPException, match="same-origin policy"):
        csrf.enforce_cookie_csrf(
            _request(
                path="/auth/citation/continue",
                origin=origin,
                fetch_site=fetch_site,
            ),
            _settings("oidc-session"),
        )


@pytest.mark.parametrize(
    ("required", "effective"),
    [
        (("documents.write",), ("documents.read",)),
        (("cdd.write",), ("cdd.read",)),
    ],
)
def test_modes_4_and_5_read_scopes_cannot_authorize_mutations(
    monkeypatch: pytest.MonkeyPatch,
    required: tuple[str, ...],
    effective: tuple[str, ...],
) -> None:
    for mode in ("oauth-access-token", "embedded-grant"):
        monkeypatch.setattr(deps, "get_settings", lambda mode=mode: _settings(mode))
        dependency = require_scopes(*required)
        with pytest.raises(HTTPException) as rejected:
            dependency(_context(scopes=effective))
        assert rejected.value.status_code == 403


@pytest.mark.parametrize("mode", ["local-persona", "iap", "oidc-session"])
def test_non_oauth_domain_scope_policies_are_explicit(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    monkeypatch.setattr(deps, "get_settings", lambda: _settings(mode))
    principal = require_scopes("cdd.write")(_context())
    assert principal.subject == "actor-1"
