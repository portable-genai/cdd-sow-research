"""End-to-end embed-broker grant exchange over the real FastAPI router.

Guarded by ``pytest.importorskip("jwt")``: needs the optional ``oidc`` extra, so the
SDK-free ``[dev]``-only gate skips this module while the ``oidc`` CI job runs it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

jwt = pytest.importorskip("jwt")

# `cryptography` arrives with the oidc extra's `pyjwt[crypto]`, so it is imported after the guard.
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from cdd_sow_research.adapters.local.browser_flow_store import (  # noqa: E402
    LocalSQLiteBrowserFlowStore,
)
from cdd_sow_research.adapters.oidc import jwks_verify  # noqa: E402
from cdd_sow_research.adapters.oidc.embed_token import (  # noqa: E402
    EmbedTokenIssuer,
    EmbedTokenKey,
    EmbedTokenPolicy,
)
from cdd_sow_research.adapters.oidc.private_key_jwt import (  # noqa: E402
    PinnedClientKey,
    PrivateKeyJwtClientPolicy,
    PrivateKeyJwtVerifier,
    SQLiteClientAssertionReplayStore,
)
from cdd_sow_research.api.embed import (  # noqa: E402
    BffGrantClientPolicy,
    BrokerInstallationPolicy,
    EmbedBrokerDependencies,
    InMemoryFixedWindowRateLimiter,
    Rfc9068BrokerSubjectTokenVerifier,
    StaticBrokerInstallationResolver,
    create_embed_router,
)
from cdd_sow_research.config import AccessTokenIssuerSettings  # noqa: E402
from cdd_sow_research.domain.browser_flow import BrowserFlowState, pkce_s256  # noqa: E402
from cdd_sow_research.domain.identity import IdentityError  # noqa: E402


def _keys():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = (
        private.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    return private, private_pem, public_pem, public_jwk


def test_real_broker_chain_uses_p3_verifier_and_issues_separate_token_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    bff_private, _bff_private_pem, _bff_public_pem, bff_jwk = _keys()
    subject_private, _subject_private_pem, _subject_public_pem, subject_jwk = _keys()
    _embed_private, embed_private_pem, embed_public_pem, _embed_jwk = _keys()
    bff_jwk.update({"kid": "bff-key", "alg": "RS256", "use": "sig"})
    subject_jwk.update({"kid": "subject-key", "alg": "RS256", "use": "sig"})
    monkeypatch.setattr(
        jwks_verify,
        "signing_jwk",
        lambda uri, kid: (
            subject_jwk
            if uri == "https://idp.demo-bank.example/jwks" and kid == "subject-key"
            else {}
        ),
    )

    broker_audience = "https://doc1.example/agent/api/v1/embed/grants"
    subject_policy = AccessTokenIssuerSettings(
        policy_id="demo-bank-broker",
        issuer="https://idp.demo-bank.example",
        jwks_uri="https://idp.demo-bank.example/jwks",
        resource_audience=broker_audience,
        tenant="demo-bank",
        allowed_clients=("portal-subject-client",),
        required_scopes=("embed.grant",),
        client_installations={"portal-subject-client": "inst_demo_bank"},
    )
    installation = BrokerInstallationPolicy(
        installation_id="inst_demo_bank",
        tenant="demo-bank",
        protocol_versions=("1",),
        parent_origins=("https://portal.demo-bank.example",),
        permitted_scopes=("cdd.read",),
        subject_grant_scope="embed.grant",
        subject_token_audience=broker_audience,
        subject_token_policy=subject_policy,
        bff_clients=(
            BffGrantClientPolicy(
                client_id="demo-bank-portal-bff",
                permitted_scopes=("cdd.read",),
                allowed_subject_clients=("portal-subject-client",),
            ),
        ),
    )
    bff_verifier = PrivateKeyJwtVerifier(
        (
            PrivateKeyJwtClientPolicy(
                client_id="demo-bank-portal-bff",
                audience=broker_audience,
                keys=(
                    PinnedClientKey(
                        kid="bff-key",
                        algorithm="RS256",
                        public_jwk=bff_jwk,
                    ),
                ),
            ),
        ),
        SQLiteClientAssertionReplayStore(tmp_path / "bff-replay.sqlite3"),
    )
    environment = {
        "DOC1_EMBED_PRIVATE": embed_private_pem,
        "DOC1_EMBED_PUBLIC": embed_public_pem,
    }
    token_issuer = EmbedTokenIssuer(
        EmbedTokenPolicy(
            issuer="https://doc1-embed.example",
            audience="https://doc1-embed.example/agent/api",
            active_kid="embed-key",
            keys=(
                EmbedTokenKey(
                    kid="embed-key",
                    algorithm="RS256",
                    private_key_env="DOC1_EMBED_PRIVATE",
                    public_key_env="DOC1_EMBED_PUBLIC",
                ),
            ),
        ),
        environment=environment.get,
        token_id_factory=lambda: "T" * 22,
    )
    store = LocalSQLiteBrowserFlowStore(tmp_path / "browser-flow.sqlite3")
    dependencies = EmbedBrokerDependencies(
        store=store,
        installations=StaticBrokerInstallationResolver((installation,)),
        subject_tokens=Rfc9068BrokerSubjectTokenVerifier(),
        bff_assertions=bff_verifier,
        token_issuer=token_issuer,
        rate_limiter=InMemoryFixedWindowRateLimiter(max_attempts=20),
        clock=lambda: now,
    )
    app = FastAPI()
    app.include_router(create_embed_router(dependencies))
    client = TestClient(app, client=("127.0.0.1", 50000))

    subject_token = jwt.encode(
        {
            "iss": "https://idp.demo-bank.example",
            "sub": "fictional-subject-123",
            "aud": broker_audience,
            "iat": int((now - timedelta(seconds=1)).timestamp()),
            "exp": int((now + timedelta(seconds=90)).timestamp()),
            "jti": "S" * 22,
            "client_id": "portal-subject-client",
            "tenant": "demo-bank",
            "scope": "embed.grant cdd.read",
            "installation_id": "inst_demo_bank",
        },
        subject_private,
        algorithm="RS256",
        headers={"kid": "subject-key", "typ": "at+jwt"},
    )
    client_assertion = jwt.encode(
        {
            "iss": "demo-bank-portal-bff",
            "sub": "demo-bank-portal-bff",
            "aud": broker_audience,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=60)).timestamp()),
            "jti": "B" * 22,
        },
        bff_private,
        algorithm="RS256",
        headers={"kid": "bff-key", "typ": "JWT"},
    )
    verifier = "A" * 43
    registered = client.post(
        "/v1/embed/instances",
        json={
            "installation_id": "inst_demo_bank",
            "protocol_version": "1",
            "pkce_challenge": pkce_s256(verifier),
            "pkce_method": "S256",
        },
    )
    assert registered.status_code == 201, registered.text
    instance_id = registered.json()["instance_id"]
    granted = client.post(
        "/v1/embed/grants",
        json={
            "installation_id": "inst_demo_bank",
            "instance_id": instance_id,
            "client_id": "demo-bank-portal-bff",
            "client_assertion_type": ("urn:ietf:params:oauth:client-assertion-type:jwt-bearer"),
            "client_assertion": client_assertion,
            "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "subject_token": subject_token,
            "requested_scopes": ["cdd.read"],
            "host_proof": {
                "host_origin": "https://portal.demo-bank.example",
                "fetch_site": "same-origin",
                "csrf_verified": True,
                "session_binding": "a" * 64,
                "session_source_subject": "fictional-subject-123",
                "user_intent_id": "intent-fictional-001",
            },
        },
    )
    assert granted.status_code == 200, granted.text
    redeemed = client.post(
        "/v1/embed/token",
        json={
            "installation_id": "inst_demo_bank",
            "instance_id": instance_id,
            "launch_code": granted.json()["launch_code"],
            "pkce_verifier": verifier,
        },
    )
    assert redeemed.status_code == 200, redeemed.text
    embedded_token = redeemed.json()["access_token"]
    verified = token_issuer.verify(embedded_token, as_of=now)

    assert verified.source_issuer == "https://idp.demo-bank.example"
    assert verified.source_subject == "fictional-subject-123"
    assert verified.client_id == "demo-bank-portal-bff"
    assert verified.scopes == ("cdd.read",)
    assert jwt.get_unverified_header(embedded_token)["typ"] == "at+jwt"
    assert (
        jwt.decode(embedded_token, options={"verify_signature": False})["token_use"]
        == "doc1-embedded-grant"
    )
    assert {event.state for event in store.pending_outbox()} == {
        BrowserFlowState.REGISTERED,
        BrowserFlowState.CODE_ISSUED,
        BrowserFlowState.CONSUMED,
    }
    with pytest.raises(IdentityError):
        token_issuer.verify(subject_token, as_of=now)
    database_text = (
        (tmp_path / "browser-flow.sqlite3").read_bytes().decode("utf-8", errors="ignore")
    )
    replay_text = (tmp_path / "bff-replay.sqlite3").read_bytes().decode("utf-8", errors="ignore")
    assert subject_token not in database_text
    assert client_assertion not in database_text
    assert client_assertion not in replay_text
