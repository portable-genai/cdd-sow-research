"""Negative matrix for the Mode 5 Google ID-token subject profile.

Guarded by ``pytest.importorskip("jwt")``: needs the optional ``oidc`` extra, so the
SDK-free ``[dev]``-only gate skips this module while the ``oidc`` CI job runs it.

Only the exact reviewed tuple (issuer, audience, authorised party, hosted domain, validity
window, plain-JWT media type) verifies. Every single-field deviation must fail closed, and
an RFC 9068 access token presented as an ID token must fail on its ``typ``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

jwt = pytest.importorskip("jwt")

# `cryptography` arrives with the oidc extra's `pyjwt[crypto]`, so it is imported after the guard.
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

from cdd_sow_research.adapters.oidc import jwks_verify  # noqa: E402
from cdd_sow_research.adapters.oidc.id_token_subject import (  # noqa: E402
    GoogleIdTokenBrokerSubjectVerifier,
)
from cdd_sow_research.api.embed import (  # noqa: E402
    BffGrantClientPolicy,
    BrokerInstallationPolicy,
)
from cdd_sow_research.config import (  # noqa: E402
    AccessTokenIssuerSettings,
    IdTokenSubjectIssuerSettings,
)
from cdd_sow_research.domain.browser_flow import ID_TOKEN_SUBJECT_TYPE  # noqa: E402
from cdd_sow_research.domain.identity import IdentityError  # noqa: E402

# PyJWT validates `iat`/`exp` against the real clock, so the fixtures are anchored to it
# (the same choice `tests/unit/test_embed_broker_integration.py` makes). `as_of` still
# exercises the adapter's own window check independently.
NOW = datetime.now(UTC).replace(microsecond=0)
GOOGLE_CLIENT = "111222333444-fictional.apps.googleusercontent.example"
JWKS_URI = "https://www.googleapis.example/oauth2/v3/certs"


@pytest.fixture(scope="module")
def signing_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(autouse=True)
def _pinned_jwks(monkeypatch: pytest.MonkeyPatch, signing_key: rsa.RSAPrivateKey) -> None:
    public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key()))
    public_jwk.update({"kid": "google-key", "alg": "RS256", "use": "sig"})
    monkeypatch.setattr(
        jwks_verify,
        "signing_jwk",
        lambda uri, kid: public_jwk if uri == JWKS_URI and kid == "google-key" else {},
    )


def _policy(**changes: object) -> IdTokenSubjectIssuerSettings:
    values: dict[str, object] = {
        "policy_id": "acme-google-subject",
        "issuer": "https://accounts.google.example",
        "jwks_uri": JWKS_URI,
        "audience": GOOGLE_CLIENT,
        "authorized_party": GOOGLE_CLIENT,
        "hosted_domain": "acme.example",
        "tenant": "acme-bank",
        "algorithms": ("RS256",),
    }
    values.update(changes)
    return IdTokenSubjectIssuerSettings(**values)  # type: ignore[arg-type]


def _installation(policy: IdTokenSubjectIssuerSettings | None = None) -> BrokerInstallationPolicy:
    resolved = policy or _policy()
    return BrokerInstallationPolicy(
        installation_id="inst_acme",
        tenant="acme-bank",
        protocol_versions=("1",),
        parent_origins=("https://portal.acme.example",),
        permitted_scopes=("cdd.read",),
        subject_grant_scope="cdd.embed",
        subject_token_audience=resolved.audience,
        subject_token_policy=resolved,
        bff_clients=(
            BffGrantClientPolicy(
                client_id="acme-portal-bff",
                permitted_scopes=("cdd.read",),
                allowed_subject_clients=(GOOGLE_CLIENT,),
            ),
        ),
        subject_token_type=ID_TOKEN_SUBJECT_TYPE,
    )


def _id_token(
    signing_key: rsa.RSAPrivateKey,
    *,
    headers: dict[str, object] | None = None,
    **changes: object,
) -> str:
    claims: dict[str, object] = {
        "iss": "https://accounts.google.example",
        "sub": "104729000000000000001",
        "aud": GOOGLE_CLIENT,
        "azp": GOOGLE_CLIENT,
        "hd": "acme.example",
        "email": "fictional.analyst@acme.example",
        "iat": int((NOW - timedelta(seconds=30)).timestamp()),
        "exp": int((NOW + timedelta(minutes=30)).timestamp()),
    }
    claims.update(changes)
    return jwt.encode(
        claims,
        signing_key,
        algorithm="RS256",
        headers={"kid": "google-key", **(headers or {})},
    )


def test_the_exact_reviewed_tuple_verifies_and_asserts_no_scopes(
    signing_key: rsa.RSAPrivateKey,
) -> None:
    verified = GoogleIdTokenBrokerSubjectVerifier().verify(
        _id_token(signing_key),
        installation=_installation(),
        as_of=NOW,
    )

    assert verified.issuer == "https://accounts.google.example"
    assert verified.source_subject == "104729000000000000001"
    # The authorised party becomes the subject's authorised client.
    assert verified.authorized_client == GOOGLE_CLIENT
    assert verified.tenant == "acme-bank"
    # An ID token carries no scope claim, so the verifier asserts none.
    assert verified.scopes == ()
    assert verified.signed_installation == ""
    assert verified.expires_at > NOW >= verified.issued_at


@pytest.mark.parametrize(
    ("claims", "headers"),
    [
        pytest.param({"iss": "https://accounts.attacker.example"}, None, id="wrong-issuer"),
        pytest.param({"aud": "other-client.apps.googleusercontent.example"}, None, id="wrong-aud"),
        pytest.param({"azp": "other-client.apps.googleusercontent.example"}, None, id="wrong-azp"),
        pytest.param({"hd": "attacker.example"}, None, id="wrong-hosted-domain"),
        pytest.param(
            {
                "iat": int((NOW - timedelta(hours=2)).timestamp()),
                "exp": int((NOW - timedelta(hours=1)).timestamp()),
            },
            None,
            id="expired",
        ),
        pytest.param({}, {"typ": "at+jwt"}, id="access-token-presented-as-id-token"),
    ],
)
def test_every_single_field_deviation_fails_closed(
    signing_key: rsa.RSAPrivateKey,
    claims: dict[str, object],
    headers: dict[str, object] | None,
) -> None:
    token = _id_token(signing_key, headers=headers, **claims)

    with pytest.raises(IdentityError):
        GoogleIdTokenBrokerSubjectVerifier().verify(
            token,
            installation=_installation(),
            as_of=NOW,
        )


def test_a_missing_hosted_domain_claim_is_refused(signing_key: rsa.RSAPrivateKey) -> None:
    claims = {
        "iss": "https://accounts.google.example",
        "sub": "104729000000000000001",
        "aud": GOOGLE_CLIENT,
        "azp": GOOGLE_CLIENT,
        "iat": int((NOW - timedelta(seconds=30)).timestamp()),
        "exp": int((NOW + timedelta(minutes=30)).timestamp()),
    }
    token = jwt.encode(claims, signing_key, algorithm="RS256", headers={"kid": "google-key"})

    with pytest.raises(IdentityError):
        GoogleIdTokenBrokerSubjectVerifier().verify(token, installation=_installation(), as_of=NOW)


def test_the_id_token_verifier_refuses_an_access_token_installation() -> None:
    access_policy = AccessTokenIssuerSettings(
        policy_id="institution-subject",
        issuer="https://id.fictionalbank.example",
        jwks_uri="https://id.fictionalbank.example/jwks",
        resource_audience="https://broker.fictionalbank.example",
        tenant="acme-bank",
        allowed_clients=("portal-subject-client",),
        required_scopes=("cdd.embed",),
    )
    installation = BrokerInstallationPolicy(
        installation_id="inst_acme",
        tenant="acme-bank",
        protocol_versions=("1",),
        parent_origins=("https://portal.acme.example",),
        permitted_scopes=("cdd.read",),
        subject_grant_scope="cdd.embed",
        subject_token_audience="https://broker.fictionalbank.example",
        subject_token_policy=access_policy,
        bff_clients=(
            BffGrantClientPolicy(
                client_id="acme-portal-bff",
                permitted_scopes=("cdd.read",),
                allowed_subject_clients=("portal-subject-client",),
            ),
        ),
    )

    with pytest.raises(IdentityError, match="does not accept an ID-token subject"):
        GoogleIdTokenBrokerSubjectVerifier().verify(
            "not.a.token", installation=installation, as_of=NOW
        )


def test_an_installation_policy_shape_must_match_its_accepted_token_type() -> None:
    with pytest.raises(ValueError, match="policy shape must match"):
        BrokerInstallationPolicy(
            installation_id="inst_acme",
            tenant="acme-bank",
            protocol_versions=("1",),
            parent_origins=("https://portal.acme.example",),
            permitted_scopes=("cdd.read",),
            subject_grant_scope="cdd.embed",
            subject_token_audience=GOOGLE_CLIENT,
            subject_token_policy=_policy(),
            bff_clients=(
                BffGrantClientPolicy(
                    client_id="acme-portal-bff",
                    permitted_scopes=("cdd.read",),
                    allowed_subject_clients=(GOOGLE_CLIENT,),
                ),
            ),
        )
