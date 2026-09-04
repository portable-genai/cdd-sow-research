"""Mode 5 embed composition wiring assembled from real settings and the real app.

Guarded by ``pytest.importorskip("jwt")``: needs the optional ``oidc`` extra, so the
SDK-free ``[dev]``-only gate skips this module while the ``oidc`` CI job runs it.
"""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("jwt")

# `cryptography` arrives with the oidc extra's `pyjwt[crypto]`, so it is imported after the guard.
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from cdd_sow_research.adapters.oidc.configured_embed_identity import (  # noqa: E402
    ConfiguredEmbeddedGrantAuthenticationAdapter,
    embed_token_issuer,
)
from cdd_sow_research.api import deps  # noqa: E402
from cdd_sow_research.api.embed_composition import (  # noqa: E402
    client_assertion_replay_store,
    embed_rate_limiter,
)
from cdd_sow_research.api.security import EmbeddedGrantApiAuthenticationAdapter  # noqa: E402
from cdd_sow_research.config import (  # noqa: E402
    AccessTokenIssuerSettings,
    ChannelSettings,
    Container,
    EmbeddedGrantBffClientSettings,
    EmbeddedGrantBffKeySettings,
    EmbeddedGrantInstallationSettings,
    EmbeddedGrantSettings,
    EmbedTokenKeySettings,
    EmbedTokenSettings,
    IdentitySettings,
    LocalSettings,
    ManifestVerifierSettings,
    Settings,
)
from cdd_sow_research.domain.browser_flow import (  # noqa: E402
    BrowserFlowState,
    GrantAuthorization,
    GrantFlowRegistration,
    authorize_grant_flow,
    hash_opaque_token,
    new_grant_flow,
    transition_grant_flow,
)
from cdd_sow_research.domain.identity import IdentityError, RequestContext  # noqa: E402

NOW = datetime.now(UTC)
PRIVATE_ENV = "TEST_MODE5_PRIVATE_PEM"
PUBLIC_ENV = "TEST_MODE5_PUBLIC_PEM"


def _b64(value: int, length: int) -> str:
    return base64.urlsafe_b64encode(value.to_bytes(length, "big")).decode().rstrip("=")


def _rsa_material() -> tuple[dict[str, str], str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = key.public_key().public_numbers()
    jwk = {
        "kty": "RSA",
        "kid": "mode5-key",
        "alg": "RS256",
        "use": "sig",
        "n": _b64(numbers.n, 256),
        "e": _b64(numbers.e, 3),
    }
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return jwk, private_pem, public_pem


def _manifest(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "deployment_manifest_id": "mode5-app-proof",
                "build_id": "mode5-build-proof",
                "installations": {
                    "inst_demo_bank": {
                        "tenant": "demo-bank",
                        "parent_origins": ["https://portal.demo-bank.example"],
                        "resource_audience": "https://agent.example/api",
                        "scopes": ["cdd.read", "documents.read"],
                        "identity_mode": "embedded-grant",
                        "issuer_policy_id": "mode5-structural",
                        "allowed_clients": ["portal-bff"],
                        "protocol_versions": ["1"],
                        "public_origin": "https://agent.example",
                        "public_mount_path": "/agent",
                        "loader_version": "v1",
                        "fallback_url": "https://standalone.example/agent/",
                    }
                },
            }
        )
    )
    return path


def _settings(tmp_path: Path, public_jwk: dict[str, str]) -> Settings:
    base = Settings.load("config/settings.yaml")
    broker_audience = "https://agent.example/agent/api/v1/embed/subject-token"
    subject_policy = AccessTokenIssuerSettings(
        policy_id="mode5-subject",
        issuer="https://idp.demo-bank.example",
        jwks_uri="https://idp.demo-bank.example/jwks",
        resource_audience=broker_audience,
        tenant="demo-bank",
        allowed_clients=("subject-client",),
        required_scopes=("embed.grant",),
        client_installations={"subject-client": "inst_demo_bank"},
    )
    bff = EmbeddedGrantBffClientSettings(
        client_id="portal-bff",
        grant_endpoint_audience=("https://agent.example/agent/api/v1/embed/grants"),
        permitted_scopes=("cdd.read", "documents.read"),
        allowed_subject_clients=("subject-client",),
        keys=(
            EmbeddedGrantBffKeySettings(
                kid="mode5-key",
                algorithm="RS256",
                public_jwk=public_jwk,
            ),
        ),
    )
    embedded = EmbeddedGrantSettings(
        subject_token_issuers=(subject_policy,),
        installations=(
            EmbeddedGrantInstallationSettings(
                installation_id="inst_demo_bank",
                subject_policy_id="mode5-subject",
                subject_token_audience=broker_audience,
                subject_grant_scope="embed.grant",
                bff_clients=(bff,),
            ),
        ),
        token=EmbedTokenSettings(
            issuer="https://agent.example/embed-token",
            audience="https://agent.example/api",
            active_kid="mode5-key",
            keys=(
                EmbedTokenKeySettings(
                    kid="mode5-key",
                    algorithm="RS256",
                    public_key_env=PUBLIC_ENV,
                    private_key_env=PRIVATE_ENV,
                ),
            ),
        ),
    )
    return Settings(
        **{
            **base.__dict__,
            "local": LocalSettings(
                browser_flow_path=str(tmp_path / "browser-flow.sqlite3"),
                client_assertion_replay_path=str(tmp_path / "bff-replay.sqlite3"),
            ),
            "identity": IdentitySettings(
                mode="embedded-grant",
                embedded_grant=embedded,
                bindings=base.identity.bindings,
            ),
            "channel": ChannelSettings(
                mode="sandboxed",
                public_origin="https://agent.example",
                installation_manifest=str(_manifest(tmp_path / "manifest.json")),
                manifest_version="v1",
                verifier_policies=(
                    ManifestVerifierSettings(
                        policy_id="mode5-structural",
                        identity_mode="embedded-grant",
                        credential_type="subject-access-token",
                        issuer="https://idp.demo-bank.example",
                        resource_audience="https://agent.example/api",
                        tenant="demo-bank",
                        allowed_clients=("portal-bff",),
                        permitted_scopes=("cdd.read", "documents.read"),
                    ),
                ),
            ),
        }
    )


@pytest.fixture
def mode5(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Settings:
    public_jwk, private_pem, public_pem = _rsa_material()
    monkeypatch.setenv(PRIVATE_ENV, private_pem)
    monkeypatch.setenv(PUBLIC_ENV, public_pem)
    return _settings(tmp_path, public_jwk)


def test_app_mounts_broker_only_during_exact_mode5_lifespan(
    mode5: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cdd_sow_research.api.app import app

    container = Container(mode5)
    monkeypatch.setattr(deps, "get_container", lambda: container)
    monkeypatch.setattr(deps, "get_settings", lambda: mode5)
    baseline = len(app.router.routes)
    ui_manifest_digest = mode5.installation_manifest().sha256

    with TestClient(app, base_url="https://agent.example", client=("127.0.0.1", 50000)) as client:
        manifest_path = Path(mode5.channel.installation_manifest)
        manifest_path.write_text(manifest_path.read_text() + "\n")
        api_manifest_digest = mode5.installation_manifest().sha256
        assert api_manifest_digest != ui_manifest_digest
        drifted = client.post(
            "/v1/embed/instances",
            headers={
                "X-CDD-Installation-ID": "inst_demo_bank",
                "X-CDD-Manifest-SHA256": ui_manifest_digest,
            },
            json={
                "installation_id": "inst_demo_bank",
                "protocol_version": "1",
                "pkce_challenge": "A" * 43,
                "pkce_method": "S256",
            },
        )
        response = client.post(
            "/v1/embed/instances",
            headers={
                "X-CDD-Installation-ID": "inst_demo_bank",
                "X-CDD-Manifest-SHA256": api_manifest_digest,
            },
            json={
                "installation_id": "inst_demo_bank",
                "protocol_version": "1",
                "pkce_challenge": "A" * 43,
                "pkce_method": "S256",
            },
        )
        health = client.get("/healthz")

    assert drifted.status_code == 409
    assert drifted.json()["detail"] == "installation manifest binding does not match"
    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    assert health.json()["deployment_manifest_id"] == "mode5-app-proof"
    assert health.json()["build_id"] == "mode5-build-proof"
    assert len(health.json()["manifest_sha256"]) == 64
    assert len(app.router.routes) == baseline


def test_startup_rejects_missing_signing_key(
    mode5: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(PRIVATE_ENV)

    with pytest.raises(ValueError, match="private signing key"):
        mode5.validate_deployment()


def test_managed_profile_selects_shared_store(mode5: Settings) -> None:
    # A managed profile with the documented placeholder project is not a valid managed
    # configuration, so name one: the loader refuses the placeholder where adapters call a
    # real cloud API, and a fixture that skipped it was describing a deployment that could
    # not exist.
    managed = replace(mode5, profile="gcp", project_id="bank-doc1-prod")

    managed.validate_deployment()

    assert managed.adapters["browser_flow_store"]["gcp"].endswith(
        "firestore_browser_flow_store:FirestoreBrowserFlowStoreAdapter"
    )
    limiter = embed_rate_limiter(managed)
    assert limiter.__class__.__name__ == "FirestoreFixedWindowRateLimiter"
    assert limiter._max_attempts == 600


@pytest.mark.parametrize("profile", ["live", "onprem"])
def test_mode5_refuses_profiles_without_reviewed_shared_replay(
    mode5: Settings,
    profile: str,
) -> None:
    unsupported = replace(mode5, profile=profile)

    with pytest.raises(NotImplementedError, match="no reviewed shared"):
        client_assertion_replay_store(unsupported)


def test_production_mode5_requires_non_exportable_kms_signer(
    mode5: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "CDD_EXPECTED_MANIFEST_SHA256",
        mode5.installation_manifest().sha256,
    )
    exported = replace(
        mode5,
        profile="gcp",
        project_id="bank-doc1-prod",
        deployment=replace(mode5.deployment, production=True, replica_count=2),
    )

    with pytest.raises(ValueError, match="managed KMS key version"):
        exported.validate_deployment()

    configured_token = mode5.identity.embedded_grant.token
    active = configured_token.keys[0]
    kms_token = replace(
        configured_token,
        keys=(
            replace(
                active,
                private_key_env="",
                kms_key_version=(
                    "projects/demo/locations/asia-southeast1/keyRings/doc1/"
                    "cryptoKeys/embed-signing/cryptoKeyVersions/1"
                ),
            ),
        ),
    )
    managed = replace(
        exported,
        identity=replace(
            exported.identity,
            embedded_grant=replace(
                exported.identity.embedded_grant,
                token=kms_token,
            ),
        ),
    )

    managed.validate_deployment()

    wrong_region_key = replace(
        kms_token.keys[0],
        kms_key_version=kms_token.keys[0].kms_key_version.replace(
            "asia-southeast1", "europe-west2"
        ),
    )
    wrong_region = replace(
        managed,
        identity=replace(
            managed.identity,
            embedded_grant=replace(
                managed.identity.embedded_grant,
                token=replace(kms_token, keys=(wrong_region_key,)),
            ),
        ),
    )
    with pytest.raises(ValueError, match="deployment region"):
        wrong_region.validate_deployment()


def test_startup_rejects_conflated_broker_and_resource_audience(mode5: Settings) -> None:
    installation = mode5.identity.embedded_grant.installations[0]
    conflated = replace(
        installation,
        subject_token_audience=mode5.identity.embedded_grant.token.audience,
    )
    identity = replace(
        mode5.identity,
        embedded_grant=replace(
            mode5.identity.embedded_grant,
            installations=(conflated,),
            subject_token_issuers=(
                replace(
                    mode5.identity.embedded_grant.subject_token_issuers[0],
                    resource_audience=mode5.identity.embedded_grant.token.audience,
                ),
            ),
        ),
    )

    with pytest.raises(ValueError, match="distinct from cdd-sow-research"):
        replace(mode5, identity=identity).validate_deployment()


def test_embedded_identity_normalizes_exact_upstream_provenance(mode5: Settings) -> None:
    registration = GrantFlowRegistration(
        installation_id="inst_demo_bank",
        tenant="demo-bank",
        protocol_version="1",
        pkce_challenge="A" * 43,
        correlation_id="mode5-provenance",
    )
    registered = new_grant_flow(
        record_id="grant-record-1",
        instance_hash=hash_opaque_token("opaque-instance-id-1234567890"),
        registration=registration,
        created_at=NOW,
        expires_at=NOW + timedelta(seconds=120),
    )
    authorized = authorize_grant_flow(
        registered,
        GrantAuthorization(
            installation_id="inst_demo_bank",
            client_id="portal-bff",
            source_issuer="https://idp.demo-bank.example",
            source_subject="subject-123",
            tenant="demo-bank",
            scopes=("cdd.read", "documents.read"),
            subject_expires_at=NOW + timedelta(seconds=120),
        ),
        code_hash=hash_opaque_token("opaque-launch-code-123456789"),
        as_of=NOW + timedelta(seconds=1),
    )
    consumed = transition_grant_flow(
        authorized,
        BrowserFlowState.CONSUMED,
        as_of=NOW + timedelta(seconds=2),
    )
    token = embed_token_issuer(mode5).mint(
        consumed,
        as_of=NOW + timedelta(seconds=3),
    )
    adapter = EmbeddedGrantApiAuthenticationAdapter(
        ConfiguredEmbeddedGrantAuthenticationAdapter(mode5)
    )

    authenticated = adapter.authenticate(
        RequestContext(
            headers={
                "authorization": f"Bearer {token.access_token}",
                "x-cdd-installation-id": "inst_demo_bank",
            }
        )
    )

    assert authenticated.principal.tenant == "demo-bank"
    assert authenticated.evidence.issuer == "https://idp.demo-bank.example"
    assert authenticated.evidence.source_subject == "subject-123"
    assert authenticated.evidence.authorized_client == "portal-bff"
    assert authenticated.evidence.installation == "inst_demo_bank"
    assert authenticated.evidence.effective_scopes == (
        "cdd.read",
        "documents.read",
    )


def test_embedded_identity_reloads_manifest_and_rejects_stale_effective_scopes(
    mode5: Settings,
) -> None:
    registration = GrantFlowRegistration(
        installation_id="inst_demo_bank",
        tenant="demo-bank",
        protocol_version="1",
        pkce_challenge="A" * 43,
        correlation_id="mode5-manifest-change",
    )
    registered = new_grant_flow(
        record_id="grant-record-manifest-change",
        instance_hash=hash_opaque_token("opaque-instance-manifest-change"),
        registration=registration,
        created_at=NOW,
        expires_at=NOW + timedelta(seconds=120),
    )
    authorized = authorize_grant_flow(
        registered,
        GrantAuthorization(
            installation_id="inst_demo_bank",
            client_id="portal-bff",
            source_issuer="https://idp.demo-bank.example",
            source_subject="subject-123",
            tenant="demo-bank",
            scopes=("cdd.read", "documents.read"),
            subject_expires_at=NOW + timedelta(seconds=120),
        ),
        code_hash=hash_opaque_token("opaque-code-manifest-change"),
        as_of=NOW + timedelta(seconds=1),
    )
    consumed = transition_grant_flow(
        authorized,
        BrowserFlowState.CONSUMED,
        as_of=NOW + timedelta(seconds=2),
    )
    token = embed_token_issuer(mode5).mint(consumed, as_of=NOW + timedelta(seconds=3))
    adapter = ConfiguredEmbeddedGrantAuthenticationAdapter(mode5)

    manifest_path = Path(mode5.channel.installation_manifest)
    manifest = json.loads(manifest_path.read_text())
    manifest["installations"]["inst_demo_bank"]["scopes"] = ["cdd.read"]
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(
        IdentityError,
        match="scopes are no longer allowed by installation policy",
    ):
        adapter.authenticate(
            RequestContext(
                headers={
                    "authorization": f"Bearer {token.access_token}",
                    "x-cdd-installation-id": "inst_demo_bank",
                }
            )
        )
