from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cdd_sow_research.adapters.local.browser_flow_store import LocalSQLiteBrowserFlowStore
from cdd_sow_research.adapters.oidc.embed_token import MintedEmbedToken
from cdd_sow_research.adapters.oidc.private_key_jwt import VerifiedBffClient
from cdd_sow_research.api.embed import (
    BffGrantClientPolicy,
    BrokerInstallationPolicy,
    EmbedBrokerDependencies,
    InMemoryFixedWindowRateLimiter,
    StaticBrokerInstallationResolver,
    VerifiedBrokerSubject,
    create_embed_router,
)
from cdd_sow_research.config import AccessTokenIssuerSettings, IdTokenSubjectIssuerSettings
from cdd_sow_research.domain.browser_flow import (
    ACCESS_TOKEN_SUBJECT_TYPE,
    ID_TOKEN_SUBJECT_TYPE,
    BrowserFlowState,
    pkce_s256,
)
from cdd_sow_research.domain.identity import IdentityError

NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
PKCE_VERIFIER = "A" * 43
CLIENT_ASSERTION = "private-key-jwt.secret.parts"
SUBJECT_TOKEN = "subject-token.secret.parts"
GOOGLE_CLIENT = "111222333444-fictional.apps.googleusercontent.example"


class FixedClock:
    def __init__(self) -> None:
        self.value = NOW

    def __call__(self) -> datetime:
        return self.value


class FakeBffVerifier:
    def __init__(self, *, reject: bool = False) -> None:
        self.reject = reject

    def verify(self, assertion: str, *, expected_client_id: str, as_of: datetime):
        if self.reject or assertion != CLIENT_ASSERTION:
            raise IdentityError("BFF assertion rejected")
        return VerifiedBffClient(
            client_id=expected_client_id,
            assertion_correlation="b" * 64,
            expires_at=as_of + timedelta(seconds=60),
        )


class FakeSubjectVerifier:
    def __init__(self, subject: VerifiedBrokerSubject | None = None) -> None:
        self.subject = subject or _subject()

    def verify(self, token: str, *, installation, as_of: datetime):
        del installation, as_of
        if token != SUBJECT_TOKEN:
            raise IdentityError("subject token rejected")
        return self.subject


class FakeTokenIssuer:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.records = []

    def mint(self, record, *, as_of: datetime) -> MintedEmbedToken:
        self.records.append(record)
        if self.fail:
            raise RuntimeError("simulated signer outage")
        return MintedEmbedToken(
            access_token="doc1-access-token.secret.parts",
            expires_at=as_of + timedelta(seconds=300),
            correlation="t" * 64,
        )


def _subject(**changes: object) -> VerifiedBrokerSubject:
    values: dict[str, object] = {
        "issuer": "https://idp.demo-bank.example",
        "source_subject": "fictional-subject-123",
        "authorized_client": "portal-subject-client",
        "tenant": "demo-bank",
        "scopes": ("embed.grant", "cdd.read", "documents.read"),
        "signed_installation": "inst_demo_bank",
        "correlation": "s" * 64,
        "issued_at": NOW - timedelta(seconds=1),
        "expires_at": NOW + timedelta(seconds=90),
    }
    values.update(changes)
    return VerifiedBrokerSubject(**values)  # type: ignore[arg-type]


def _installation() -> BrokerInstallationPolicy:
    subject_policy = AccessTokenIssuerSettings(
        policy_id="demo-bank-broker",
        issuer="https://idp.demo-bank.example",
        jwks_uri="https://idp.demo-bank.example/jwks",
        resource_audience="https://doc1.example/launch-broker",
        tenant="demo-bank",
        allowed_clients=("portal-subject-client",),
        required_scopes=("embed.grant",),
        client_installations={"portal-subject-client": "inst_demo_bank"},
    )
    return BrokerInstallationPolicy(
        installation_id="inst_demo_bank",
        tenant="demo-bank",
        protocol_versions=("1",),
        parent_origins=("https://portal.demo-bank.example",),
        permitted_scopes=("cdd.read", "documents.read"),
        subject_grant_scope="embed.grant",
        subject_token_audience="https://doc1.example/launch-broker",
        subject_token_policy=subject_policy,
        bff_clients=(
            BffGrantClientPolicy(
                client_id="demo-bank-portal-bff",
                permitted_scopes=("cdd.read",),
                allowed_subject_clients=("portal-subject-client",),
            ),
        ),
    )


def _google_installation() -> BrokerInstallationPolicy:
    """The same installation, reviewed to accept the Google ID-token subject profile."""
    subject_policy = IdTokenSubjectIssuerSettings(
        policy_id="demo-bank-google-subject",
        issuer="https://accounts.google.example",
        jwks_uri="https://www.googleapis.example/oauth2/v3/certs",
        audience=GOOGLE_CLIENT,
        authorized_party=GOOGLE_CLIENT,
        hosted_domain="demo-bank.example",
        tenant="demo-bank",
    )
    return BrokerInstallationPolicy(
        installation_id="inst_demo_bank",
        tenant="demo-bank",
        protocol_versions=("1",),
        parent_origins=("https://portal.demo-bank.example",),
        permitted_scopes=("cdd.read", "documents.read"),
        subject_grant_scope="embed.grant",
        subject_token_audience=GOOGLE_CLIENT,
        subject_token_policy=subject_policy,
        bff_clients=(
            BffGrantClientPolicy(
                client_id="demo-bank-portal-bff",
                permitted_scopes=("cdd.read",),
                allowed_subject_clients=("portal-subject-client",),
            ),
        ),
        subject_token_type=ID_TOKEN_SUBJECT_TYPE,
    )


def _client(
    tmp_path: Path,
    *,
    bff=None,
    subject=None,
    token_issuer=None,
    rate_limiter=None,
    installation=None,
):
    store = LocalSQLiteBrowserFlowStore(tmp_path / "broker-flows.sqlite3")
    clock = FixedClock()
    issuer = token_issuer or FakeTokenIssuer()
    dependencies = EmbedBrokerDependencies(
        store=store,
        installations=StaticBrokerInstallationResolver((installation or _installation(),)),
        subject_tokens=subject or FakeSubjectVerifier(),
        bff_assertions=bff or FakeBffVerifier(),
        token_issuer=issuer,
        rate_limiter=rate_limiter or InMemoryFixedWindowRateLimiter(max_attempts=20),
        clock=clock,
    )
    app = FastAPI()
    app.include_router(create_embed_router(dependencies))
    return TestClient(app, client=("127.0.0.1", 50000)), store, clock, issuer


def _register(client: TestClient) -> dict[str, object]:
    response = client.post(
        "/v1/embed/instances",
        json={
            "installation_id": "inst_demo_bank",
            "protocol_version": "1",
            "pkce_challenge": pkce_s256(PKCE_VERIFIER),
            "pkce_method": "S256",
        },
        headers={"X-Request-ID": "integration-request-001"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _host_proof(**changes: object) -> dict[str, object]:
    proof: dict[str, object] = {
        "host_origin": "https://portal.demo-bank.example",
        "fetch_site": "same-origin",
        "csrf_verified": True,
        "session_binding": "a" * 64,
        "session_source_subject": "fictional-subject-123",
        "user_intent_id": "intent-fictional-001",
    }
    proof.update(changes)
    return proof


def _grant(client: TestClient, instance_id: str, **changes: object):
    request: dict[str, object] = {
        "installation_id": "inst_demo_bank",
        "instance_id": instance_id,
        "client_id": "demo-bank-portal-bff",
        "client_assertion_type": ("urn:ietf:params:oauth:client-assertion-type:jwt-bearer"),
        "client_assertion": CLIENT_ASSERTION,
        "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "subject_token": SUBJECT_TOKEN,
        "requested_scopes": ["cdd.read", "admin.write"],
        "host_proof": _host_proof(),
    }
    request.update(changes)
    return client.post("/v1/embed/grants", json=request)


def _redeem(client: TestClient, instance_id: str, launch_code: str):
    return client.post(
        "/v1/embed/token",
        json={
            "installation_id": "inst_demo_bank",
            "instance_id": instance_id,
            "launch_code": launch_code,
            "pkce_verifier": PKCE_VERIFIER,
        },
    )


def test_broker_routes_complete_flow_with_no_store_and_scope_intersection(
    tmp_path: Path,
) -> None:
    client, store, _clock, issuer = _client(tmp_path)

    registered_response = client.post(
        "/v1/embed/instances",
        json={
            "installation_id": "inst_demo_bank",
            "protocol_version": "1",
            "pkce_challenge": pkce_s256(PKCE_VERIFIER),
            "pkce_method": "S256",
        },
    )
    assert registered_response.status_code == 201
    assert registered_response.headers["cache-control"] == "no-store"
    registered = registered_response.json()

    grant_response = _grant(client, registered["instance_id"])
    assert grant_response.status_code == 200, grant_response.text
    assert grant_response.headers["cache-control"] == "no-store"
    granted = grant_response.json()

    token_response = _redeem(client, registered["instance_id"], granted["launch_code"])

    assert token_response.status_code == 200, token_response.text
    assert token_response.headers["cache-control"] == "no-store"
    assert token_response.json() == {
        "access_token": "doc1-access-token.secret.parts",
        "token_type": "Bearer",
        "expires_in": 300,
        "scope": "cdd.read",
    }
    assert issuer.records[0].authorization is not None
    assert issuer.records[0].authorization.scopes == ("cdd.read",)
    assert {event.state for event in store.pending_outbox()} == {
        BrowserFlowState.REGISTERED,
        BrowserFlowState.CODE_ISSUED,
        BrowserFlowState.CONSUMED,
    }


@pytest.mark.parametrize(
    "proof",
    [
        _host_proof(host_origin="https://attacker.example"),
        _host_proof(fetch_site="cross-site"),
        _host_proof(csrf_verified=False),
        _host_proof(session_source_subject="other-subject"),
        _host_proof(session_binding="not-a-hash"),
        _host_proof(user_intent_id="short"),
    ],
)
def test_broker_rejects_unbound_host_session_controls(
    tmp_path: Path, proof: dict[str, object]
) -> None:
    client, store, _clock, _issuer = _client(tmp_path)
    registered = _register(client)

    response = _grant(client, str(registered["instance_id"]), host_proof=proof)

    assert response.status_code == 403
    assert response.headers["cache-control"] == "no-store"
    assert store.pending_outbox()[0].state is BrowserFlowState.REGISTERED
    assert len(store.pending_outbox()) == 1


@pytest.mark.parametrize(
    "subject",
    [
        _subject(tenant="other-bank"),
        _subject(authorized_client="unregistered-subject-client"),
        _subject(signed_installation="inst_other"),
        _subject(scopes=("cdd.read",)),
    ],
)
def test_broker_rejects_subject_tenant_client_installation_or_scope(
    tmp_path: Path, subject: VerifiedBrokerSubject
) -> None:
    client, store, _clock, _issuer = _client(tmp_path, subject=FakeSubjectVerifier(subject))
    registered = _register(client)

    response = _grant(client, str(registered["instance_id"]))

    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"
    assert len(store.pending_outbox()) == 1


def test_broker_error_never_echoes_assertion_or_subject_token(tmp_path: Path) -> None:
    client, _store, _clock, _issuer = _client(tmp_path, bff=FakeBffVerifier(reject=True))
    registered = _register(client)

    response = _grant(client, str(registered["instance_id"]))

    assert response.status_code == 401
    assert CLIENT_ASSERTION not in response.text
    assert SUBJECT_TOKEN not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_broker_validation_error_is_generic_no_store_and_does_not_echo_secret(
    tmp_path: Path,
) -> None:
    client, _store, _clock, _issuer = _client(tmp_path)
    registered = _register(client)
    secret = "invalid-secret-that-must-not-be-echoed"

    response = _grant(
        client,
        str(registered["instance_id"]),
        client_assertion={"unexpected": secret},
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "embed request rejected"}
    assert secret not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_signer_failure_after_consume_is_durable_and_retry_cannot_remint(
    tmp_path: Path,
) -> None:
    failing_issuer = FakeTokenIssuer(fail=True)
    client, store, _clock, _issuer = _client(tmp_path, token_issuer=failing_issuer)
    registered = _register(client)
    granted = _grant(client, str(registered["instance_id"])).json()

    failed = _redeem(
        client,
        str(registered["instance_id"]),
        granted["launch_code"],
    )
    retried = _redeem(
        client,
        str(registered["instance_id"]),
        granted["launch_code"],
    )

    assert failed.status_code == 503
    assert retried.status_code == 409
    assert {event.state for event in store.pending_outbox()} == {
        BrowserFlowState.REGISTERED,
        BrowserFlowState.CODE_ISSUED,
        BrowserFlowState.CONSUMED,
    }


def test_broker_rate_limit_hook_fails_closed(tmp_path: Path) -> None:
    limiter = InMemoryFixedWindowRateLimiter(max_attempts=1, window_seconds=60)
    client, _store, _clock, _issuer = _client(tmp_path, rate_limiter=limiter)
    first = _register(client)

    second = client.post(
        "/v1/embed/instances",
        json={
            "installation_id": "inst_demo_bank",
            "protocol_version": "1",
            "pkce_challenge": pkce_s256("B" * 43),
            "pkce_method": "S256",
        },
    )

    assert first["state"] == "REGISTERED"
    assert second.status_code == 429
    assert second.headers["cache-control"] == "no-store"


def test_an_id_token_installation_accepts_only_its_reviewed_subject_type(
    tmp_path: Path,
) -> None:
    client, _store, _clock, _issuer = _client(tmp_path, installation=_google_installation())
    registered = _register(client)

    accepted = _grant(
        client,
        str(registered["instance_id"]),
        subject_token_type=ID_TOKEN_SUBJECT_TYPE,
    )
    refused = _grant(
        client,
        str(registered["instance_id"]),
        subject_token_type=ACCESS_TOKEN_SUBJECT_TYPE,
    )

    assert accepted.status_code == 200, accepted.text
    assert refused.status_code == 401


def test_an_access_token_installation_still_refuses_an_id_token_grant(tmp_path: Path) -> None:
    client, store, _clock, _issuer = _client(tmp_path)
    registered = _register(client)

    response = _grant(
        client,
        str(registered["instance_id"]),
        subject_token_type=ID_TOKEN_SUBJECT_TYPE,
    )

    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"
    assert len(store.pending_outbox()) == 1


def test_id_token_scope_comes_from_reviewed_policy_not_from_a_token_claim(
    tmp_path: Path,
) -> None:
    # Exactly what the real Google verifier returns: no scope claim exists on an ID token.
    scopeless = FakeSubjectVerifier(_subject(scopes=()))
    client, _store, _clock, issuer = _client(
        tmp_path,
        installation=_google_installation(),
        subject=scopeless,
    )
    registered = _register(client)

    response = _grant(
        client,
        str(registered["instance_id"]),
        subject_token_type=ID_TOKEN_SUBJECT_TYPE,
    )

    assert response.status_code == 200, response.text
    assert issuer.records == []
    granted = _redeem(client, str(registered["instance_id"]), response.json()["launch_code"])
    # Exactly the reviewed installation and BFF scopes, never the requested admin.write.
    assert granted.json()["scope"] == "cdd.read"


def test_an_unpermitted_requested_scope_still_fails_on_the_empty_intersection(
    tmp_path: Path,
) -> None:
    scopeless = FakeSubjectVerifier(_subject(scopes=()))
    client, _store, _clock, _issuer = _client(
        tmp_path,
        installation=_google_installation(),
        subject=scopeless,
    )
    registered = _register(client)

    response = _grant(
        client,
        str(registered["instance_id"]),
        subject_token_type=ID_TOKEN_SUBJECT_TYPE,
        requested_scopes=["admin.write"],
    )

    assert response.status_code == 401


def test_an_access_token_subject_without_the_grant_scope_is_still_refused(
    tmp_path: Path,
) -> None:
    client, _store, _clock, _issuer = _client(
        tmp_path,
        subject=FakeSubjectVerifier(_subject(scopes=("cdd.read",))),
    )
    registered = _register(client)

    response = _grant(client, str(registered["instance_id"]))

    assert response.status_code == 401


def test_an_unreviewed_subject_token_type_is_refused_by_the_request_schema(
    tmp_path: Path,
) -> None:
    client, _store, _clock, _issuer = _client(tmp_path)
    registered = _register(client)

    response = _grant(
        client,
        str(registered["instance_id"]),
        subject_token_type="urn:ietf:params:oauth:token-type:refresh_token",
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "embed request rejected"}


def test_static_registry_rejects_cross_installation_client_mapping() -> None:
    other = replace(_installation(), installation_id="inst_other")

    with pytest.raises(ValueError, match="exactly one installation"):
        StaticBrokerInstallationResolver((_installation(), other))
