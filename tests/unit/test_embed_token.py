"""Mode 5 embed-token issuance, verification and KMS-signer tests.

Guarded by ``pytest.importorskip("jwt")``: needs the optional ``oidc`` extra, so the
SDK-free ``[dev]``-only gate skips this module while the ``oidc`` CI job runs it.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from types import ModuleType

import pytest

jwt = pytest.importorskip("jwt")

# `cryptography` arrives with the oidc extra's `pyjwt[crypto]`, so it is imported after the guard.
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

from cdd_sow_research.adapters.gcp.kms_embed_token import KmsEmbedTokenSigner  # noqa: E402
from cdd_sow_research.adapters.oidc.embed_token import (  # noqa: E402
    EmbedTokenIssuer,
    EmbedTokenKey,
    EmbedTokenPolicy,
    canonical_actor,
)
from cdd_sow_research.adapters.oidc.embed_token_identity import (  # noqa: E402
    EmbeddedGrantTokenAuthenticationAdapter,
)
from cdd_sow_research.config import Settings  # noqa: E402
from cdd_sow_research.domain.browser_flow import (  # noqa: E402
    BrowserFlowState,
    GrantAuthorization,
    GrantFlowRegistration,
    authorize_grant_flow,
    hash_opaque_token,
    new_grant_flow,
    pkce_s256,
    transition_grant_flow,
)
from cdd_sow_research.domain.identity import IdentityError, RequestContext  # noqa: E402

NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)


def _pem_pair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = (
        private_key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_key, private_pem, public_pem


def _policy(
    *,
    active_kid: str = "embed-key-1",
    keys: tuple[EmbedTokenKey, ...] | None = None,
) -> EmbedTokenPolicy:
    return EmbedTokenPolicy(
        issuer="https://doc1-embed.example",
        audience="https://doc1-embed.example/agent/api",
        active_kid=active_kid,
        keys=keys
        or (
            EmbedTokenKey(
                kid="embed-key-1",
                algorithm="RS256",
                private_key_env="EMBED_PRIVATE_1",
                public_key_env="EMBED_PUBLIC_1",
            ),
        ),
    )


def _consumed_record(*, subject_lifetime: int = 240):
    registration = GrantFlowRegistration(
        installation_id="inst_demo_bank",
        tenant="demo-bank",
        protocol_version="1",
        pkce_challenge=pkce_s256("A" * 43),
        correlation_id="grant-correlation",
    )
    registered = new_grant_flow(
        record_id="grant-record",
        instance_hash=hash_opaque_token("opaque-instance"),
        registration=registration,
        created_at=NOW,
        expires_at=NOW + timedelta(seconds=120),
    )
    authorization = GrantAuthorization(
        installation_id="inst_demo_bank",
        client_id="demo-bank-portal-bff",
        source_issuer="https://idp.demo-bank.example",
        source_subject="fictional-subject-123",
        tenant="demo-bank",
        scopes=("cdd.read", "documents.read"),
        subject_expires_at=NOW + timedelta(seconds=subject_lifetime),
    )
    issued = authorize_grant_flow(
        registered,
        authorization,
        code_hash=hash_opaque_token("launch-code"),
        as_of=NOW + timedelta(seconds=1),
    )
    return transition_grant_flow(
        issued,
        BrowserFlowState.CONSUMED,
        as_of=NOW + timedelta(seconds=2),
    )


def _issuer(*, subject_lifetime: int = 240):
    private_key, private_pem, public_pem = _pem_pair()
    environment = {
        "EMBED_PRIVATE_1": private_pem,
        "EMBED_PUBLIC_1": public_pem,
    }
    issuer = EmbedTokenIssuer(
        _policy(),
        environment=environment.get,
        token_id_factory=lambda: "J" * 22,
    )
    return issuer, private_key, environment, _consumed_record(subject_lifetime=subject_lifetime)


def test_embed_token_contains_exact_provenance_and_type() -> None:
    issuer, _private_key, _environment, record = _issuer()

    minted = issuer.mint(record, as_of=NOW + timedelta(seconds=2))
    verified = issuer.verify(minted.access_token, as_of=NOW + timedelta(seconds=3))
    header = jwt.get_unverified_header(minted.access_token)

    assert header["typ"] == "at+jwt"
    assert verified.subject == canonical_actor(
        "https://idp.demo-bank.example", "fictional-subject-123"
    )
    assert verified.source_issuer == "https://idp.demo-bank.example"
    assert verified.source_subject == "fictional-subject-123"
    assert verified.tenant == "demo-bank"
    assert verified.installation_id == "inst_demo_bank"
    assert verified.client_id == "demo-bank-portal-bff"
    assert verified.scopes == ("cdd.read", "documents.read")
    assert len(verified.correlation) == 64
    assert minted.access_token not in repr(minted)


def test_embed_token_expiry_is_truncated_to_subject_credential() -> None:
    issuer, _private_key, _environment, record = _issuer(subject_lifetime=30)

    minted = issuer.mint(record, as_of=NOW + timedelta(seconds=2))

    assert minted.expires_at == NOW + timedelta(seconds=30)
    with pytest.raises(IdentityError, match="expired"):
        issuer.verify(minted.access_token, as_of=NOW + timedelta(seconds=61))


def test_embed_token_requires_consumed_grant() -> None:
    issuer, _private_key, _environment, record = _issuer()
    not_consumed = record.__class__(
        record_id=record.record_id,
        instance_hash=record.instance_hash,
        state=BrowserFlowState.CODE_ISSUED,
        registration=record.registration,
        created_at=record.created_at,
        expires_at=record.expires_at,
        state_changed_at=record.state_changed_at,
        authorization=record.authorization,
        code_hash=record.code_hash,
        code_issued_at=record.code_issued_at,
        code_expires_at=record.code_expires_at,
    )

    with pytest.raises(IdentityError, match="consumed"):
        issuer.mint(not_consumed, as_of=NOW + timedelta(seconds=2))


@pytest.mark.parametrize(
    ("claim_changes", "message"),
    [
        ({"token_use": "session"}, "not a cdd-sow-research"),
        ({"source_iss": ""}, "source_iss"),
        ({"source_sub": ""}, "source_sub"),
        ({"sub": "attacker"}, "provenance"),
        ({"aud": "https://wrong.example/api"}, "audience"),
        (
            {
                "iat": int(NOW.timestamp()),
                "exp": int(NOW.timestamp()) + 301,
            },
            "lifetime",
        ),
        (
            {
                "iat": int((NOW + timedelta(seconds=33)).timestamp()),
                "exp": int((NOW + timedelta(seconds=60)).timestamp()),
            },
            "future",
        ),
        ({"nbf": int((NOW + timedelta(seconds=34)).timestamp())}, "not yet valid"),
    ],
)
def test_embed_token_rejects_claim_and_type_confusion(
    claim_changes: dict[str, object], message: str
) -> None:
    issuer, private_key, _environment, record = _issuer()
    minted = issuer.mint(record, as_of=NOW)
    claims = jwt.decode(minted.access_token, options={"verify_signature": False})
    claims.update(claim_changes)
    confused = jwt.encode(
        claims,
        private_key,
        algorithm="RS256",
        headers={"kid": "embed-key-1", "typ": "at+jwt"},
    )

    with pytest.raises(IdentityError, match=message):
        issuer.verify(confused, as_of=NOW)


def test_embed_token_rejects_token_selected_key_header() -> None:
    issuer, private_key, _environment, record = _issuer()
    minted = issuer.mint(record, as_of=NOW)
    claims = jwt.decode(minted.access_token, options={"verify_signature": False})
    token = jwt.encode(
        claims,
        private_key,
        algorithm="RS256",
        headers={
            "kid": "embed-key-1",
            "typ": "at+jwt",
            "jku": "https://attacker.example/jwks",
        },
    )

    with pytest.raises(IdentityError, match="token-controlled"):
        issuer.verify(token, as_of=NOW)


def test_embed_token_rotation_accepts_retired_key() -> None:
    old_private, old_private_pem, old_public_pem = _pem_pair()
    _new_private, new_private_pem, new_public_pem = _pem_pair()
    old_key = EmbedTokenKey(
        kid="old",
        algorithm="RS256",
        private_key_env="OLD_PRIVATE",
        public_key_env="OLD_PUBLIC",
    )
    new_key = EmbedTokenKey(
        kid="new",
        algorithm="RS256",
        private_key_env="NEW_PRIVATE",
        public_key_env="NEW_PUBLIC",
    )
    environment = {
        "OLD_PRIVATE": old_private_pem,
        "OLD_PUBLIC": old_public_pem,
        "NEW_PRIVATE": new_private_pem,
        "NEW_PUBLIC": new_public_pem,
    }
    old_issuer = EmbedTokenIssuer(
        _policy(active_kid="old", keys=(old_key,)),
        environment=environment.get,
        token_id_factory=lambda: "O" * 22,
    )
    token = old_issuer.mint(_consumed_record(), as_of=NOW).access_token
    rotated = EmbedTokenIssuer(
        _policy(active_kid="new", keys=(new_key, old_key)),
        environment=environment.get,
    )

    verified = rotated.verify(token, as_of=NOW + timedelta(seconds=1))

    assert verified.installation_id == "inst_demo_bank"
    assert jwt.get_unverified_header(token)["kid"] == "old"
    assert old_private is not None


def test_embed_token_can_sign_with_non_exportable_kms_key() -> None:
    private_key, _private_pem, public_pem = _pem_pair()
    key = EmbedTokenKey(
        kid="kms-active",
        algorithm="RS256",
        public_key_env="KMS_PUBLIC",
        kms_key_version=(
            "projects/demo/locations/asia-southeast1/keyRings/doc1/"
            "cryptoKeys/embed-signing/cryptoKeyVersions/1"
        ),
    )
    signer = KmsEmbedTokenSigner(Settings.load("config/settings.yaml"))
    assert signer._api_endpoint == "asia-southeast1-cloudkms.googleapis.com"

    class _Response:
        signature: bytes

    class _FakeKms:
        def asymmetric_sign(self, *, request):
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric import padding, utils

            response = _Response()
            response.signature = private_key.sign(
                request["digest"]["sha256"],
                padding.PKCS1v15(),
                utils.Prehashed(hashes.SHA256()),
            )
            return response

    signer._client = _FakeKms()
    issuer = EmbedTokenIssuer(
        _policy(active_kid="kms-active", keys=(key,)),
        environment={"KMS_PUBLIC": public_pem}.get,
        token_id_factory=lambda: "K" * 22,
        managed_signer=signer,
    )

    minted = issuer.mint(_consumed_record(), as_of=NOW)

    assert issuer.verify(minted.access_token, as_of=NOW).tenant == "demo-bank"
    assert jwt.get_unverified_header(minted.access_token)["kid"] == "kms-active"


def test_kms_signer_constructs_client_with_regional_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _ClientOptions:
        def __init__(self, *, api_endpoint: str):
            self.api_endpoint = api_endpoint

    class _Client:
        def __init__(self, *, client_options: _ClientOptions):
            captured["endpoint"] = client_options.api_endpoint

    client_options_module = ModuleType("google.api_core.client_options")
    client_options_module.ClientOptions = _ClientOptions
    kms_module = ModuleType("google.cloud.kms_v1")
    kms_module.KeyManagementServiceClient = _Client
    cloud_module = ModuleType("google.cloud")
    cloud_module.kms_v1 = kms_module
    api_core_module = ModuleType("google.api_core")
    api_core_module.client_options = client_options_module

    monkeypatch.setitem(sys.modules, "google.cloud", cloud_module)
    monkeypatch.setitem(sys.modules, "google.cloud.kms_v1", kms_module)
    monkeypatch.setitem(sys.modules, "google.api_core", api_core_module)
    monkeypatch.setitem(sys.modules, "google.api_core.client_options", client_options_module)

    signer = KmsEmbedTokenSigner(Settings.load("config/settings.yaml"))

    assert signer._kms().__class__ is _Client
    assert captured["endpoint"] == "asia-southeast1-cloudkms.googleapis.com"


def test_embedded_identity_adapter_requires_exact_installation() -> None:
    issuer, _private_key, _environment, record = _issuer()
    token = issuer.mint(record, as_of=NOW).access_token
    adapter = EmbeddedGrantTokenAuthenticationAdapter(
        issuer,
        installation_ids=("inst_demo_bank", "inst_second_portal"),
        clock=lambda: NOW,
    )
    context = RequestContext(
        headers={
            "authorization": f"Bearer {token}",
            "x-cdd-installation-id": "inst_demo_bank",
        }
    )

    identity = adapter.authenticate(context)

    assert identity.principal.tenant == "demo-bank"
    assert identity.principal.assurance == "embedded-grant"
    with pytest.raises(IdentityError, match="not enabled"):
        adapter.authenticate(
            RequestContext(
                headers={
                    "authorization": f"Bearer {token}",
                    "x-cdd-installation-id": "inst_other",
                }
            )
        )
    with pytest.raises(IdentityError, match="does not match selector"):
        adapter.authenticate(
            RequestContext(
                headers={
                    "authorization": f"Bearer {token}",
                    "x-cdd-installation-id": "inst_second_portal",
                }
            )
        )


def test_embed_token_errors_do_not_echo_token() -> None:
    issuer, _private_key, _environment, record = _issuer()
    token = issuer.mint(record, as_of=NOW).access_token + "tampered"

    with pytest.raises(IdentityError) as captured:
        issuer.verify(token, as_of=NOW)

    assert token not in str(captured.value)
