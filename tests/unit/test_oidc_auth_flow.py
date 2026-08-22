"""Unit tests for the Mode 6 ("launch in new tab") login flow: /auth/login,
/auth/callback, /auth/logout (src/cdd_sow_research/api/auth.py).

Guarded by ``pytest.importorskip("jwt")``: needs the optional ``oidc`` extra. The
SDK-free import-safety proof (importing api.app with no PyJWT installed) lives in
test_api_cli_agent_importsafe.py's existing test_api_app_imports_without_gcp_sdk, which
already exercises this module's import path for free since api/app.py mounts the auth
router.

The IdP's discovery document, JWKS, and token endpoint are mocked with respx (already a
dev dependency): api/auth.py and adapters/oidc/* call out via httpx exclusively (never
PyJWT's built-in urllib-based JWKS fetcher), so every outbound call in this flow is
mockable the same way.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Iterator
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import pytest

jwt = pytest.importorskip("jwt")

import respx  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from cdd_sow_research.adapters.oidc import discovery, jwks_verify, session_token  # noqa: E402
from cdd_sow_research.api import deps  # noqa: E402
from cdd_sow_research.api.security import canonical_actor  # noqa: E402
from cdd_sow_research.config import (  # noqa: E402
    ChannelSettings,
    IdentitySettings,
    IssuerSettings,
    Settings,
    build_container,
)

_TXN = session_token.TXN_COOKIE_NAME
_SESSION = session_token.SESSION_COOKIE_NAME

_ISSUER_URL = "https://idp.test.example"
_CLIENT_ID = "cdd-agent"
_CLIENT_SECRET_ENV = "TEST_CDD_OIDC_CLIENT_SECRET"
_SIGNING_KEY_ENV = "TEST_CDD_SESSION_SIGNING_KEY"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_CLIENT_SECRET_ENV, "unit-test-client-secret")
    monkeypatch.setenv(_SIGNING_KEY_ENV, "unit-test-signing-key-at-least-32-bytes-long")


@pytest.fixture(autouse=True)
def _clear_module_caches() -> Iterator[None]:
    # discovery/jwks_verify cache by issuer/jwks_uri; tests reuse _ISSUER_URL, so each test
    # must start from a clean cache or it could read a previous test's mocked response.
    discovery._cache.clear()
    jwks_verify._cache.clear()
    yield
    discovery._cache.clear()
    jwks_verify._cache.clear()


@pytest.fixture
def rsa_keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = key.public_key().public_numbers()

    def b64url(n: int, length: int) -> str:
        return base64.urlsafe_b64encode(n.to_bytes(length, "big")).decode("ascii").rstrip("=")

    jwk = {
        "kty": "RSA",
        "kid": "test-kid",
        "use": "sig",
        "alg": "RS256",
        "n": b64url(numbers.n, 256),
        "e": b64url(numbers.e, 3),
    }
    return key, jwk


def _issuer(**overrides: object) -> IssuerSettings:
    defaults: dict[str, object] = {
        "issuer": _ISSUER_URL,
        "tenant": "demo-bank",
        "client_id": _CLIENT_ID,
        "client_secret_env": _CLIENT_SECRET_ENV,
        "groups_claim": "groups",
        "tenant_claim": "tenant",
    }
    return IssuerSettings(**{**defaults, **overrides})


def _settings(*issuers: IssuerSettings, allowed_return_to_hosts: tuple[str, ...] = ()) -> Settings:
    base = Settings.load("config/settings.yaml")
    return Settings(
        **{
            **base.__dict__,
            "identity": IdentitySettings(
                mode="oidc-session",
                trusted_issuers=issuers,
                session_signing_key_env=_SIGNING_KEY_ENV,
                session_ttl_seconds=3600,
                allowed_return_to_hosts=allowed_return_to_hosts,
                bindings=base.identity.bindings,
            ),
            "channel": ChannelSettings(
                mode="standalone",
                public_origin="https://agent.test.example",
            ),
            "web": replace(base.web, rate_limit_per_minute=0),
        }
    )


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    from cdd_sow_research.api.app import app

    def _install(settings: Settings) -> TestClient:
        container = build_container(settings)
        monkeypatch.setattr(deps, "get_container", lambda: container)
        return TestClient(app, client=("127.0.0.1", 50000))

    return _install


def _mock_discovery() -> None:
    # Registers on respx's default global router; every call site is inside a
    # `with respx.mock:` block, so `respx.get(...)` here targets that same router.
    respx.get(f"{_ISSUER_URL}/.well-known/openid-configuration").respond(
        json={
            "issuer": _ISSUER_URL,
            "authorization_endpoint": f"{_ISSUER_URL}/authorize",
            "token_endpoint": f"{_ISSUER_URL}/token",
            "jwks_uri": f"{_ISSUER_URL}/jwks",
            "end_session_endpoint": f"{_ISSUER_URL}/logout",
            "token_endpoint_auth_methods_supported": ["client_secret_basic"],
        }
    )


def _mint_id_token(key, claims: dict[str, object]) -> str:
    now = int(time.time())
    payload = {"iat": now, "exp": now + 300, **claims}
    return jwt.encode(payload, key, algorithm="RS256", headers={"kid": "test-kid"})


# --------------------------------------------------------------------------- #
# /auth/login
# --------------------------------------------------------------------------- #
def test_login_no_issuer_configured_is_503(client) -> None:
    c = client(_settings())
    response = c.get("/auth/login", follow_redirects=False)
    assert response.status_code == 503


def test_login_redirects_with_pkce_and_state(client, rsa_keypair) -> None:
    _key, jwk = rsa_keypair
    c = client(_settings(_issuer()))
    with respx.mock:
        _mock_discovery()
        response = c.get("/auth/login?return_to=/dashboard", follow_redirects=False)
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith(f"{_ISSUER_URL}/authorize?")
    qs = parse_qs(urlsplit(location).query)
    assert qs["code_challenge_method"] == ["S256"]
    assert qs["response_type"] == ["code"]
    assert qs["client_id"] == [_CLIENT_ID]
    assert qs["redirect_uri"] == ["https://agent.test.example/agent/auth/callback"]
    assert "state" in qs and "code_challenge" in qs
    assert response.cookies.get(_TXN)
    assert "Path=/agent/auth" in response.headers["set-cookie"]


def test_login_unknown_tenant_is_404(client) -> None:
    c = client(_settings(_issuer(tenant="demo-bank")))
    response = c.get("/auth/login?tenant=nonexistent", follow_redirects=False)
    assert response.status_code == 404


def test_login_rejects_discovery_issuer_mismatch(client) -> None:
    c = client(_settings(_issuer()))
    with respx.mock:
        respx.get(f"{_ISSUER_URL}/.well-known/openid-configuration").respond(
            json={
                "issuer": "https://attacker.test.example",
                "authorization_endpoint": f"{_ISSUER_URL}/authorize",
                "token_endpoint": f"{_ISSUER_URL}/token",
                "jwks_uri": f"{_ISSUER_URL}/jwks",
            }
        )
        response = c.get("/auth/login", follow_redirects=False)

    assert response.status_code == 503
    assert "does not match configured issuer" in response.json()["detail"]


def test_login_rejects_discovered_endpoint_on_unreviewed_host(client) -> None:
    c = client(_settings(_issuer()))
    with respx.mock:
        respx.get(f"{_ISSUER_URL}/.well-known/openid-configuration").respond(
            json={
                "issuer": _ISSUER_URL,
                "authorization_endpoint": "https://attacker.test.example/authorize",
                "token_endpoint": f"{_ISSUER_URL}/token",
                "jwks_uri": f"{_ISSUER_URL}/jwks",
            }
        )
        response = c.get("/auth/login", follow_redirects=False)

    assert response.status_code == 503
    assert "reviewed issuer policy" in response.json()["detail"]


@pytest.mark.parametrize(
    "return_to",
    ["https://evil.example/steal", "//evil.example/steal", "/\\evil.example"],
)
def test_login_open_redirect_attempts_are_clamped(client, return_to: str) -> None:
    c = client(_settings(_issuer()))
    with respx.mock:
        _mock_discovery()
        response = c.get(f"/auth/login?return_to={return_to}", follow_redirects=False)
    txn = response.cookies.get(_TXN)
    claims = jwt.decode(txn, "unit-test-signing-key-at-least-32-bytes-long", algorithms=["HS256"])
    assert claims["return_to"] == "/"


def test_login_safe_relative_return_to_is_preserved(client) -> None:
    c = client(_settings(_issuer()))
    with respx.mock:
        _mock_discovery()
        response = c.get("/auth/login?return_to=/dashboard/case/123", follow_redirects=False)
    txn = response.cookies.get(_TXN)
    claims = jwt.decode(txn, "unit-test-signing-key-at-least-32-bytes-long", algorithms=["HS256"])
    assert claims["return_to"] == "/dashboard/case/123"


# --------------------------------------------------------------------------- #
# /auth/callback
# --------------------------------------------------------------------------- #
def test_callback_missing_txn_cookie_is_400(client) -> None:
    c = client(_settings(_issuer()))
    response = c.get("/auth/callback?code=abc&state=xyz", follow_redirects=False)
    assert response.status_code == 400


def test_callback_end_to_end_mints_session_cookie(client, rsa_keypair) -> None:
    key, jwk = rsa_keypair
    c = client(_settings(_issuer()))
    with respx.mock:
        _mock_discovery()
        respx.get(f"{_ISSUER_URL}/jwks").respond(json={"keys": [jwk]})

        login = c.get("/auth/login?return_to=/dashboard", follow_redirects=False)
        login_qs = parse_qs(urlsplit(login.headers["location"]).query)
        state = login_qs["state"][0]
        nonce = login_qs["nonce"][0]  # the id_token must echo the login nonce
        c.cookies.set(_TXN, login.cookies.get(_TXN))

        id_token = _mint_id_token(
            key,
            {
                "iss": _ISSUER_URL,
                "aud": _CLIENT_ID,
                "sub": "user-123",
                "nonce": nonce,
                "email": "demo.analyst@bank.example",
                "tenant": "demo-bank",
                "groups": ["cdd-analyst", "risk"],
                "acr": "mfa",
            },
        )
        respx.post(f"{_ISSUER_URL}/token").respond(
            json={"id_token": id_token, "access_token": "unused"}
        )

        response = c.get(f"/auth/callback?code=abc123&state={state}", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "/dashboard"
    session = response.cookies.get(_SESSION)
    assert session

    from cdd_sow_research.adapters.oidc.session_identity import OidcSessionIdentityAdapter
    from cdd_sow_research.domain.identity import RequestContext

    settings = _settings(_issuer())
    principal = OidcSessionIdentityAdapter(settings).resolve(
        RequestContext(headers={"cookie": f"{_SESSION}={session}"})
    )
    actor = canonical_actor(_ISSUER_URL, "user-123")
    assert principal.subject == actor
    assert principal.tenant == "demo-bank"
    assert set(principal.principals) == {
        "group:cdd-analyst",
        "group:risk",
        f"user:{actor}",
    }
    assert principal.assurance == "mfa"
    assert principal.source == "oidc-session"


def test_callback_rejects_tenant_claim_that_conflicts_with_reviewed_mapping(
    client, rsa_keypair
) -> None:
    key, jwk = rsa_keypair
    c = client(_settings(_issuer(tenant="reviewed-bank")))
    with respx.mock:
        _mock_discovery()
        respx.get(f"{_ISSUER_URL}/jwks").respond(json={"keys": [jwk]})
        login = c.get("/auth/login", follow_redirects=False)
        query = parse_qs(urlsplit(login.headers["location"]).query)
        c.cookies.set(_TXN, login.cookies.get(_TXN))
        id_token = _mint_id_token(
            key,
            {
                "iss": _ISSUER_URL,
                "aud": _CLIENT_ID,
                "sub": "user-123",
                "nonce": query["nonce"][0],
                "tenant": "attacker-selected-bank",
            },
        )
        respx.post(f"{_ISSUER_URL}/token").respond(json={"id_token": id_token})
        response = c.get(
            f"/auth/callback?code=abc&state={query['state'][0]}",
            follow_redirects=False,
        )

    assert response.status_code == 401
    assert response.json()["detail"] == (
        "id_token tenant does not match the reviewed issuer mapping"
    )


def test_callback_state_mismatch_is_400(client) -> None:
    c = client(_settings(_issuer()))
    with respx.mock:
        _mock_discovery()
        login = c.get("/auth/login", follow_redirects=False)
        c.cookies.set(_TXN, login.cookies.get(_TXN))
        response = c.get("/auth/callback?code=abc&state=wrong-state", follow_redirects=False)
    assert response.status_code == 400


def test_login_sends_nonce_and_session_cookie_uses_host_prefix(client) -> None:
    c = client(_settings(_issuer()))
    with respx.mock:
        _mock_discovery()
        response = c.get("/auth/login", follow_redirects=False)
    qs = parse_qs(urlsplit(response.headers["location"]).query)
    assert qs.get("nonce")  # OIDC nonce present on the authorize request
    # The txn cookie uses the browser-enforced __Secure- prefix.
    assert _TXN.startswith("__Secure-")
    assert _SESSION.startswith("__Host-")


def test_callback_id_token_nonce_mismatch_is_401(client, rsa_keypair) -> None:
    key, jwk = rsa_keypair
    c = client(_settings(_issuer()))
    with respx.mock:
        _mock_discovery()
        respx.get(f"{_ISSUER_URL}/jwks").respond(json={"keys": [jwk]})

        login = c.get("/auth/login", follow_redirects=False)
        state = parse_qs(urlsplit(login.headers["location"]).query)["state"][0]
        c.cookies.set(_TXN, login.cookies.get(_TXN))

        # Valid signature/audience/issuer but the WRONG nonce (id-token replay).
        id_token = _mint_id_token(
            key,
            {
                "iss": _ISSUER_URL,
                "aud": _CLIENT_ID,
                "sub": "user-123",
                "email": "demo.analyst@bank.example",
                "nonce": "attacker-supplied-nonce",
            },
        )
        respx.post(f"{_ISSUER_URL}/token").respond(json={"id_token": id_token})
        response = c.get(f"/auth/callback?code=abc&state={state}", follow_redirects=False)
    assert response.status_code == 401


def test_session_verifies_under_rotated_key(monkeypatch) -> None:
    """A session minted under the previous key still verifies during the rotation window."""
    from cdd_sow_research.adapters.oidc import session_token as st

    monkeypatch.setenv("OLD_KEY", "old-signing-key-at-least-32-bytes-long-xx")
    monkeypatch.setenv("NEW_KEY", "new-signing-key-at-least-32-bytes-long-xx")
    token = st.mint({"sub": "u1"}, typ="session", signing_key_env="OLD_KEY", ttl_seconds=3600)
    # After rotation NEW_KEY mints; OLD_KEY is still accepted.
    claims = st.verify(
        token, typ="session", signing_key_env="NEW_KEY", accepted_key_envs=("OLD_KEY",)
    )
    assert claims["sub"] == "u1"
    # Once OLD_KEY is retired from the accepted list, the old token no longer verifies.
    from cdd_sow_research.domain.identity import IdentityError

    with pytest.raises(IdentityError):
        st.verify(token, typ="session", signing_key_env="NEW_KEY")


def test_non_https_issuer_is_rejected_at_config_load() -> None:
    from cdd_sow_research.config import _require_https_issuer

    _require_https_issuer("https://idp.example.com")  # ok
    _require_https_issuer("http://localhost:9000")  # loopback dev ok
    with pytest.raises(ValueError, match="non-https"):
        _require_https_issuer("http://idp.example.com")


def test_callback_id_token_wrong_audience_is_401(client, rsa_keypair) -> None:
    key, jwk = rsa_keypair
    c = client(_settings(_issuer()))
    with respx.mock:
        _mock_discovery()
        respx.get(f"{_ISSUER_URL}/jwks").respond(json={"keys": [jwk]})

        login = c.get("/auth/login", follow_redirects=False)
        state = parse_qs(urlsplit(login.headers["location"]).query)["state"][0]
        c.cookies.set(_TXN, login.cookies.get(_TXN))

        id_token = _mint_id_token(
            key,
            {
                "iss": _ISSUER_URL,
                "aud": "some-other-client",  # wrong audience
                "sub": "user-123",
                "email": "demo.analyst@bank.example",
            },
        )
        respx.post(f"{_ISSUER_URL}/token").respond(json={"id_token": id_token})

        response = c.get(f"/auth/callback?code=abc&state={state}", follow_redirects=False)
    assert response.status_code == 401


def test_callback_id_token_alg_none_is_401(client) -> None:
    """An unsigned (alg=none) token, however it arrived, must never be accepted."""
    c = client(_settings(_issuer()))
    with respx.mock:
        _mock_discovery()
        respx.get(f"{_ISSUER_URL}/jwks").respond(json={"keys": []})

        login = c.get("/auth/login", follow_redirects=False)
        state = parse_qs(urlsplit(login.headers["location"]).query)["state"][0]
        c.cookies.set(_TXN, login.cookies.get(_TXN))

        unsigned = jwt.encode(
            {"iss": _ISSUER_URL, "aud": _CLIENT_ID, "sub": "user-123"},
            key="",
            algorithm="none",
        )
        respx.post(f"{_ISSUER_URL}/token").respond(json={"id_token": unsigned})

        response = c.get(f"/auth/callback?code=abc&state={state}", follow_redirects=False)
    assert response.status_code == 401


def test_callback_jwks_fetch_failure_fails_closed(client, rsa_keypair) -> None:
    key, _jwk = rsa_keypair
    c = client(_settings(_issuer()))
    with respx.mock:
        _mock_discovery()
        respx.get(f"{_ISSUER_URL}/jwks").respond(status_code=500)

        login = c.get("/auth/login", follow_redirects=False)
        state = parse_qs(urlsplit(login.headers["location"]).query)["state"][0]
        c.cookies.set(_TXN, login.cookies.get(_TXN))

        id_token = _mint_id_token(key, {"iss": _ISSUER_URL, "aud": _CLIENT_ID, "sub": "user-123"})
        respx.post(f"{_ISSUER_URL}/token").respond(json={"id_token": id_token})

        response = c.get(f"/auth/callback?code=abc&state={state}", follow_redirects=False)
    assert response.status_code == 401


def test_callback_token_endpoint_failure_is_502(client) -> None:
    c = client(_settings(_issuer()))
    with respx.mock:
        _mock_discovery()
        login = c.get("/auth/login", follow_redirects=False)
        state = parse_qs(urlsplit(login.headers["location"]).query)["state"][0]
        c.cookies.set(_TXN, login.cookies.get(_TXN))

        respx.post(f"{_ISSUER_URL}/token").respond(status_code=500)
        response = c.get(f"/auth/callback?code=abc&state={state}", follow_redirects=False)
    assert response.status_code == 502


# --------------------------------------------------------------------------- #
# /auth/logout
# --------------------------------------------------------------------------- #
def test_logout_clears_session_cookie(client) -> None:
    settings = _settings(_issuer())
    c = client(settings)
    actor = canonical_actor(_ISSUER_URL, "user-123")
    session = session_token.mint(
        {
            "sub": actor,
            "source_sub": "user-123",
            "issuer": _ISSUER_URL,
            "tenant": "demo-bank",
            "principals": [f"user:{actor}"],
            "jti": "logout-session",
        },
        typ="session",
        signing_key_env=_SIGNING_KEY_ENV,
        ttl_seconds=3600,
    )
    c.cookies.set(_SESSION, session)
    csrf_response = c.get("/auth/csrf?method=POST&path=/auth/logout")
    response = c.post(
        "/auth/logout",
        follow_redirects=False,
        headers={
            "Origin": "https://agent.test.example",
            "Sec-Fetch-Site": "same-origin",
            "X-CSRF-Token": csrf_response.json()["csrf_token"],
        },
    )
    assert response.status_code == 302
    set_cookie = response.headers.get("set-cookie", "")
    assert f'{_SESSION}=""' in set_cookie
    # The deletion Set-Cookie must carry Secure + Path=/, or a browser rejects it for a
    # __Host--prefixed name and the session survives logout (regression guard).
    lowered = set_cookie.lower()
    assert "secure" in lowered
    assert "path=/" in lowered


def test_logout_with_no_session_cookie_still_succeeds(client) -> None:
    c = client(_settings(_issuer()))
    response = c.post("/auth/logout", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/"
