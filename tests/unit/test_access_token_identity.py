"""Mode 4 RFC 9068 access-token authentication and adversarial policy tests.

Guarded by ``pytest.importorskip("jwt")``: needs the optional ``oidc`` extra, so the
SDK-free ``[dev]``-only gate skips this module while the ``oidc`` CI job runs it.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

jwt = pytest.importorskip("jwt")

# `cryptography` arrives with the oidc extra's `pyjwt[crypto]`, so it is imported after the
# guard: the SDK-free `[dev]`-only gate must skip this module, not fail collecting it.
from cryptography.hazmat.primitives.asymmetric import ec, rsa  # noqa: E402

from cdd_sow_research.adapters.oidc import jwks_verify  # noqa: E402
from cdd_sow_research.adapters.oidc.access_token_identity import (  # noqa: E402
    OAuthAccessTokenAuthenticationAdapter,
)
from cdd_sow_research.api.security import (  # noqa: E402
    OAuthAccessTokenApiAuthenticationAdapter,
    canonical_actor,
)
from cdd_sow_research.config import (  # noqa: E402
    AccessTokenIssuerSettings,
    ChannelSettings,
    Settings,
)
from cdd_sow_research.domain.identity import IdentityError, RequestContext  # noqa: E402

_ISSUER = "https://idp.demo-bank.example"
_JWKS = f"{_ISSUER}/jwks.json"
_AGENT_ORIGIN = "https://doc1.bank-agent.example"
_PARENT_ORIGIN = "https://portal.demo-bank.example"
_AUDIENCE = "https://doc1.example/api"
_CLIENT = "demo-bank-portal"
_TENANT = "demo-bank"
_INSTALLATION = "inst_demo_bank"
_SCOPES = ("cdd.read", "cdd.write")


def _b64(value: int, size: int) -> str:
    return base64.urlsafe_b64encode(value.to_bytes(size, "big")).decode().rstrip("=")


def _rsa_key() -> tuple[Any, dict[str, str], str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = key.public_key().public_numbers()
    return (
        key,
        {
            "kty": "RSA",
            "kid": "rsa-1",
            "use": "sig",
            "alg": "RS256",
            "n": _b64(numbers.n, 256),
            "e": _b64(numbers.e, 3),
        },
        "RS256",
    )


def _ec_key() -> tuple[Any, dict[str, str], str]:
    key = ec.generate_private_key(ec.SECP256R1())
    numbers = key.public_key().public_numbers()
    return (
        key,
        {
            "kty": "EC",
            "kid": "ec-1",
            "use": "sig",
            "alg": "ES256",
            "crv": "P-256",
            "x": _b64(numbers.x, 32),
            "y": _b64(numbers.y, 32),
        },
        "ES256",
    )


@pytest.fixture(autouse=True)
def _clear_jwks_cache() -> None:
    jwks_verify._cache.clear()


def _manifest(path: Path, *, identity_mode: str = "oauth-access-token") -> Path:
    document = {
        "schema_version": 1,
        "deployment_manifest_id": "doc1-mode4-test",
        "build_id": "doc1-test-build",
        "installations": {
            _INSTALLATION: {
                "tenant": _TENANT,
                "parent_origins": [_PARENT_ORIGIN],
                "resource_audience": _AUDIENCE,
                "scopes": list(_SCOPES),
                "identity_mode": identity_mode,
                "issuer_policy_id": "demo-bank-direct",
                "allowed_clients": [_CLIENT],
                "protocol_versions": ["1"],
                "public_origin": _AGENT_ORIGIN,
                "public_mount_path": "/agent",
                "loader_version": "v1",
                "fallback_url": "https://doc1-standalone.example/agent/",
            }
        },
    }
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _settings(
    tmp_path: Path,
    *,
    algorithm: str,
    policy_changes: dict[str, Any] | None = None,
) -> Settings:
    base = Settings.load("config/settings.yaml")
    values: dict[str, Any] = {
        "policy_id": "demo-bank-direct",
        "issuer": _ISSUER,
        "jwks_uri": _JWKS,
        "resource_audience": _AUDIENCE,
        "tenant": _TENANT,
        "algorithms": (algorithm,),
        "allowed_clients": (_CLIENT,),
        "required_scopes": _SCOPES,
        "max_lifetime_seconds": 300,
        "clock_skew_seconds": 30,
    }
    values.update(policy_changes or {})
    policy = AccessTokenIssuerSettings(**values)
    manifest = _manifest(tmp_path / "installations.json")
    return replace(
        base,
        local=replace(
            base.local,
            browser_flow_path=str(tmp_path / "browser-flow.sqlite3"),
        ),
        identity=replace(
            base.identity,
            mode="oauth-access-token",
            access_token_issuers=(policy,),
        ),
        channel=ChannelSettings(
            mode="sandboxed",
            public_origin=_AGENT_ORIGIN,
            installation_manifest=str(manifest),
            manifest_version="test-v1",
        ),
    )


def test_managed_mode4_selects_shared_citation_store(tmp_path: Path) -> None:
    # A managed profile needs a real project: the loader refuses the documented placeholder
    # where every adapter calls a live cloud API.
    settings = replace(
        _settings(tmp_path, algorithm="RS256"), profile="gcp", project_id="bank-doc1-prod"
    )

    settings.validate_deployment()

    assert settings.adapters["browser_flow_store"]["gcp"].endswith(
        "firestore_browser_flow_store:FirestoreBrowserFlowStoreAdapter"
    )


def _claims(**changes: Any) -> dict[str, Any]:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": _ISSUER,
        "sub": "source-user-123",
        "aud": _AUDIENCE,
        "client_id": _CLIENT,
        "tenant": _TENANT,
        "scope": " ".join(_SCOPES),
        "installation_id": _INSTALLATION,
        "groups": ["cdd-analyst"],
        "jti": "raw-jti-must-not-escape",
        "iat": now - 1,
        "exp": now + 299,
    }
    claims.update(changes)
    return claims


def _token(key: Any, algorithm: str, kid: str, *, claims: dict[str, Any] | None = None, **header):
    return jwt.encode(
        claims or _claims(),
        key,
        algorithm=algorithm,
        headers={"kid": kid, "typ": "at+jwt", **header},
    )


def _context(token: str, **headers: str) -> RequestContext:
    return RequestContext(
        headers={
            "authorization": f"Bearer {token}",
            "x-cdd-installation-id": _INSTALLATION,
            "origin": _AGENT_ORIGIN,
            ":method": "POST",
            **headers,
        }
    )


@pytest.mark.parametrize("key_factory", [_rsa_key, _ec_key], ids=["rsa-issuer", "ec-issuer"])
def test_two_synthetic_issuers_verify_and_normalize_context(tmp_path: Path, key_factory) -> None:
    key, jwk, algorithm = key_factory()
    issuer = _ISSUER if algorithm == "RS256" else "https://idp2.demo-bank.example"
    jwks_uri = _JWKS if algorithm == "RS256" else f"{issuer}/jwks.json"
    settings = _settings(
        tmp_path,
        algorithm=algorithm,
        policy_changes={"issuer": issuer, "jwks_uri": jwks_uri},
    )
    settings.validate_deployment()
    verifier = OAuthAccessTokenAuthenticationAdapter(settings)
    token = _token(key, algorithm, jwk["kid"], claims=_claims(iss=issuer))

    with respx.mock:
        respx.get(jwks_uri).respond(json={"keys": [jwk]})
        verified = verifier.authenticate(_context(token))
        normalized = OAuthAccessTokenApiAuthenticationAdapter(verifier).authenticate(
            _context(token)
        )

    assert verified.principal.subject == canonical_actor(issuer, "source-user-123")
    assert verified.principal.tenant == _TENANT
    assert verified.authorized_client == _CLIENT
    assert verified.installation_id == _INSTALLATION
    assert verified.effective_scopes == _SCOPES
    assert verified.correlation != "raw-jti-must-not-escape"
    assert len(verified.correlation) == 64
    assert normalized.evidence.token_type == "at+jwt"
    assert normalized.evidence.installation == _INSTALLATION


def test_valid_access_token_is_reusable_before_expiry(tmp_path: Path) -> None:
    key, jwk, algorithm = _rsa_key()
    verifier = OAuthAccessTokenAuthenticationAdapter(_settings(tmp_path, algorithm=algorithm))
    token = _token(key, algorithm, jwk["kid"])

    with respx.mock:
        route = respx.get(_JWKS).respond(json={"keys": [jwk]})
        first = verifier.authenticate(_context(token))
        second = verifier.authenticate(_context(token))

    assert first == second
    assert route.call_count == 1


def test_jwks_rotation_refetches_once_for_a_new_kid(tmp_path: Path) -> None:
    old_key, old_jwk, algorithm = _rsa_key()
    new_key, new_jwk, _ = _rsa_key()
    new_jwk["kid"] = "rsa-rotated"
    verifier = OAuthAccessTokenAuthenticationAdapter(_settings(tmp_path, algorithm=algorithm))
    old_token = _token(old_key, algorithm, old_jwk["kid"])
    new_token = _token(new_key, algorithm, new_jwk["kid"])

    with respx.mock:
        route = respx.get(_JWKS)
        route.side_effect = [
            httpx.Response(200, json={"keys": [old_jwk]}),
            httpx.Response(200, json={"keys": [old_jwk, new_jwk]}),
        ]
        verifier.authenticate(_context(old_token))
        verifier.authenticate(_context(new_token))

    assert route.call_count == 2


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"iss": "https://attacker.example"}, "verification failed"),
        ({"aud": "https://other-resource.example"}, "verification failed"),
        ({"client_id": "unreviewed-client"}, "client is not allowed"),
        ({"tenant": "other-bank"}, "tenant does not match"),
        ({"scope": "cdd.read"}, "missing required"),
        ({"installation_id": "inst_other"}, "installation claim does not match"),
        ({"iat": int(time.time()) + 120}, "verification failed"),
        ({"nbf": int(time.time()) + 120}, "verification failed"),
        ({"exp": int(time.time()) - 120}, "verification failed"),
    ],
)
def test_claim_policy_failures_are_rejected(
    tmp_path: Path, changes: dict[str, Any], message: str
) -> None:
    key, jwk, algorithm = _rsa_key()
    verifier = OAuthAccessTokenAuthenticationAdapter(_settings(tmp_path, algorithm=algorithm))
    token = _token(key, algorithm, jwk["kid"], claims=_claims(**changes))

    with respx.mock, pytest.raises(IdentityError, match=message):
        respx.get(_JWKS).respond(json={"keys": [jwk]})
        verifier.authenticate(_context(token))


def test_excessive_lifetime_is_rejected_after_signature_verification(tmp_path: Path) -> None:
    key, jwk, algorithm = _rsa_key()
    now = int(time.time())
    verifier = OAuthAccessTokenAuthenticationAdapter(_settings(tmp_path, algorithm=algorithm))
    token = _token(
        key,
        algorithm,
        jwk["kid"],
        claims=_claims(iat=now, exp=now + 301),
    )

    with respx.mock, pytest.raises(IdentityError, match="lifetime exceeds"):
        respx.get(_JWKS).respond(json={"keys": [jwk]})
        verifier.authenticate(_context(token))


@pytest.mark.parametrize(
    "claim",
    ["iss", "sub", "aud", "client_id", "tenant", "scope", "iat", "exp"],
)
def test_every_required_rfc9068_claim_is_enforced(tmp_path: Path, claim: str) -> None:
    key, jwk, algorithm = _rsa_key()
    verifier = OAuthAccessTokenAuthenticationAdapter(_settings(tmp_path, algorithm=algorithm))
    claims = _claims()
    del claims[claim]
    token = _token(key, algorithm, jwk["kid"], claims=claims)

    with respx.mock, pytest.raises(IdentityError):
        respx.get(_JWKS).respond(json={"keys": [jwk]})
        verifier.authenticate(_context(token))


def test_wrong_signing_key_is_rejected(tmp_path: Path) -> None:
    _trusted_key, trusted_jwk, algorithm = _rsa_key()
    attacker_key, _attacker_jwk, _ = _rsa_key()
    verifier = OAuthAccessTokenAuthenticationAdapter(_settings(tmp_path, algorithm=algorithm))
    token = _token(attacker_key, algorithm, trusted_jwk["kid"])

    with respx.mock, pytest.raises(IdentityError, match="verification failed"):
        respx.get(_JWKS).respond(json={"keys": [trusted_jwk]})
        verifier.authenticate(_context(token))


def test_missing_or_unknown_installation_context_fails_before_token_use(tmp_path: Path) -> None:
    key, jwk, algorithm = _rsa_key()
    verifier = OAuthAccessTokenAuthenticationAdapter(_settings(tmp_path, algorithm=algorithm))
    token = _token(key, algorithm, jwk["kid"])

    with pytest.raises(IdentityError, match="requires X-CDD-Installation-ID"):
        verifier.authenticate(
            RequestContext(
                headers={
                    "authorization": f"Bearer {token}",
                    "origin": _AGENT_ORIGIN,
                    ":method": "POST",
                }
            )
        )
    with pytest.raises(IdentityError, match="unknown Mode 4 installation"):
        verifier.authenticate(_context(token, **{"x-cdd-installation-id": "inst_unknown"}))


@pytest.mark.parametrize("typ", ["JWT", "id+jwt", "session"])
def test_token_type_confusion_is_rejected_before_jwks_fetch(tmp_path: Path, typ: str) -> None:
    key, jwk, algorithm = _rsa_key()
    verifier = OAuthAccessTokenAuthenticationAdapter(_settings(tmp_path, algorithm=algorithm))
    token = _token(key, algorithm, jwk["kid"], typ=typ)

    with respx.mock, pytest.raises(IdentityError, match="typ must be exactly at\\+jwt"):
        route = respx.get(_JWKS).respond(json={"keys": [jwk]})
        verifier.authenticate(_context(token))

    assert route.call_count == 0


def test_token_controlled_jwks_url_is_rejected(tmp_path: Path) -> None:
    key, jwk, algorithm = _rsa_key()
    verifier = OAuthAccessTokenAuthenticationAdapter(_settings(tmp_path, algorithm=algorithm))
    token = _token(
        key,
        algorithm,
        jwk["kid"],
        jku="https://attacker.example/jwks.json",
    )

    with respx.mock, pytest.raises(IdentityError, match="token-controlled"):
        route = respx.get(_JWKS).respond(json={"keys": [jwk]})
        verifier.authenticate(_context(token))

    assert route.call_count == 0


def test_unpinned_algorithm_is_rejected_before_jwks_fetch(tmp_path: Path) -> None:
    settings = _settings(tmp_path, algorithm="RS256")
    verifier = OAuthAccessTokenAuthenticationAdapter(settings)
    token = jwt.encode(
        _claims(),
        "synthetic-test-secret-at-least-32-bytes",
        algorithm="HS256",
        headers={"kid": "attacker", "typ": "at+jwt"},
    )

    with respx.mock, pytest.raises(IdentityError, match="algorithm is not allowed"):
        route = respx.get(_JWKS).respond(json={"keys": []})
        verifier.authenticate(_context(token))

    assert route.call_count == 0


def test_wrong_origin_and_missing_unsafe_origin_are_rejected(tmp_path: Path) -> None:
    key, jwk, algorithm = _rsa_key()
    verifier = OAuthAccessTokenAuthenticationAdapter(_settings(tmp_path, algorithm=algorithm))
    token = _token(key, algorithm, jwk["kid"])

    with pytest.raises(IdentityError, match="Origin does not match"):
        verifier.authenticate(_context(token, origin="https://attacker.example"))
    with pytest.raises(IdentityError, match="requires the exact agent Origin"):
        verifier.authenticate(_context(token, origin=""))


def test_reviewed_client_mapping_can_bind_a_token_without_installation_claim(
    tmp_path: Path,
) -> None:
    key, jwk, algorithm = _rsa_key()
    settings = _settings(
        tmp_path,
        algorithm=algorithm,
        policy_changes={"client_installations": {_CLIENT: _INSTALLATION}},
    )
    verifier = OAuthAccessTokenAuthenticationAdapter(settings)
    claims = _claims()
    del claims["installation_id"]
    token = _token(key, algorithm, jwk["kid"], claims=claims)

    with respx.mock:
        respx.get(_JWKS).respond(json={"keys": [jwk]})
        verified = verifier.authenticate(_context(token))

    assert verified.installation_id == _INSTALLATION


def test_failures_and_repr_never_include_the_bearer_or_raw_jti(tmp_path: Path) -> None:
    key, jwk, algorithm = _rsa_key()
    verifier = OAuthAccessTokenAuthenticationAdapter(_settings(tmp_path, algorithm=algorithm))
    token = _token(key, algorithm, jwk["kid"], claims=_claims(tenant="wrong"))

    with respx.mock, pytest.raises(IdentityError) as raised:
        respx.get(_JWKS).respond(json={"keys": [jwk]})
        verifier.authenticate(_context(token))

    assert token not in str(raised.value)
    assert "raw-jti-must-not-escape" not in str(raised.value)
    assert token not in repr(verifier)
