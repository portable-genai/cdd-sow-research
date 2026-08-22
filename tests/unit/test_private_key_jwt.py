"""private_key_jwt client-assertion verification and replay-store tests.

Guarded by ``pytest.importorskip("jwt")``: needs the optional ``oidc`` extra, so the
SDK-free ``[dev]``-only gate skips this module while the ``oidc`` CI job runs it.
"""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

jwt = pytest.importorskip("jwt")

# `cryptography` arrives with the oidc extra's `pyjwt[crypto]`, so it is imported after the guard.
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

from cdd_sow_research.adapters.oidc.private_key_jwt import (  # noqa: E402
    InMemoryClientAssertionReplayStore,
    PinnedClientKey,
    PrivateKeyJwtClientPolicy,
    PrivateKeyJwtVerifier,
    SQLiteClientAssertionReplayStore,
)
from cdd_sow_research.domain.identity import IdentityError  # noqa: E402

NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
CLIENT_ID = "demo-bank-portal-bff"
AUDIENCE = "https://doc1.example/agent/api/v1/embed/grants"


@pytest.fixture
def rsa_keys():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    public_jwk.update({"kid": "bff-key-1", "alg": "RS256", "use": "sig"})
    return private_key, public_jwk


def _policy(public_jwk: dict[str, Any]) -> PrivateKeyJwtClientPolicy:
    return PrivateKeyJwtClientPolicy(
        client_id=CLIENT_ID,
        audience=AUDIENCE,
        keys=(
            PinnedClientKey(
                kid="bff-key-1",
                algorithm="RS256",
                public_jwk=public_jwk,
            ),
        ),
    )


def _assertion(
    private_key,
    *,
    claims: dict[str, Any] | None = None,
    headers: dict[str, Any] | None = None,
) -> str:
    now = int(NOW.timestamp())
    payload: dict[str, Any] = {
        "iss": CLIENT_ID,
        "sub": CLIENT_ID,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + 60,
        "jti": "A" * 22,
    }
    payload.update(claims or {})
    protected = {"kid": "bff-key-1", "typ": "JWT"}
    protected.update(headers or {})
    return jwt.encode(payload, private_key, algorithm="RS256", headers=protected)


def test_private_key_jwt_verifies_exact_client_and_consumes_jti(rsa_keys) -> None:
    private_key, public_jwk = rsa_keys
    replay = InMemoryClientAssertionReplayStore()
    verifier = PrivateKeyJwtVerifier((_policy(public_jwk),), replay)
    assertion = _assertion(private_key)

    verified = verifier.verify(
        assertion,
        expected_client_id=CLIENT_ID,
        as_of=NOW,
    )

    assert verified.client_id == CLIENT_ID
    assert len(verified.assertion_correlation) == 64
    assert verified.expires_at == NOW + timedelta(seconds=60)
    with pytest.raises(IdentityError, match="already been used"):
        verifier.verify(assertion, expected_client_id=CLIENT_ID, as_of=NOW)
    with pytest.raises(IdentityError, match="already been used"):
        verifier.verify(
            assertion,
            expected_client_id=CLIENT_ID,
            as_of=NOW + timedelta(seconds=70),
        )


@pytest.mark.parametrize(
    ("claims", "message"),
    [
        ({"iss": "other-client"}, "iss and sub"),
        ({"sub": "other-client"}, "iss and sub"),
        ({"aud": "https://wrong.example/grants"}, "audience"),
        ({"iat": int(NOW.timestamp()) + 31}, "future"),
        (
            {
                "iat": int(NOW.timestamp()),
                "exp": int(NOW.timestamp()) + 61,
            },
            "lifetime",
        ),
        (
            {
                "iat": int(NOW.timestamp()) - 91,
                "exp": int(NOW.timestamp()) - 31,
            },
            "expired",
        ),
        ({"jti": "short"}, "jti"),
        ({"aud": [AUDIENCE]}, "audience"),
    ],
)
def test_private_key_jwt_rejects_claim_confusion(
    rsa_keys, claims: dict[str, Any], message: str
) -> None:
    private_key, public_jwk = rsa_keys
    verifier = PrivateKeyJwtVerifier((_policy(public_jwk),), InMemoryClientAssertionReplayStore())

    with pytest.raises(IdentityError, match=message):
        verifier.verify(
            _assertion(private_key, claims=claims),
            expected_client_id=CLIENT_ID,
            as_of=NOW,
        )


@pytest.mark.parametrize(
    "headers",
    [
        {"kid": "unknown"},
        {"jku": "https://attacker.example/jwks"},
        {"x5u": "https://attacker.example/cert"},
        {"typ": "at+jwt"},
    ],
)
def test_private_key_jwt_rejects_token_selected_key_or_type(
    rsa_keys, headers: dict[str, Any]
) -> None:
    private_key, public_jwk = rsa_keys
    verifier = PrivateKeyJwtVerifier((_policy(public_jwk),), InMemoryClientAssertionReplayStore())

    with pytest.raises(IdentityError):
        verifier.verify(
            _assertion(private_key, headers=headers),
            expected_client_id=CLIENT_ID,
            as_of=NOW,
        )


def test_private_key_jwt_malformed_protected_header_is_a_caller_fault(rsa_keys) -> None:
    """With PyJWT present, a well-formed-shape token whose header is not decodable JSON is a
    genuine CALLER fault: it stays an ``IdentityError`` (HTTP 401), never the environment
    fault reserved for a missing library."""
    _, public_jwk = rsa_keys
    verifier = PrivateKeyJwtVerifier((_policy(public_jwk),), InMemoryClientAssertionReplayStore())
    # Three segments (passes the compact-shape check), but the header segment base64url-
    # decodes to bytes that are not a JSON object, so PyJWT rejects it as malformed.
    malformed = "bm90LWpzb24.eyJhIjogMX0.c2lnbmF0dXJl"

    with pytest.raises(IdentityError, match="protected header is invalid"):
        verifier.verify(malformed, expected_client_id=CLIENT_ID, as_of=NOW)


def test_private_key_jwt_replay_is_atomic_under_concurrency(rsa_keys) -> None:
    private_key, public_jwk = rsa_keys
    verifier = PrivateKeyJwtVerifier((_policy(public_jwk),), InMemoryClientAssertionReplayStore())
    assertion = _assertion(private_key)

    def attempt(_: int) -> str:
        try:
            verifier.verify(assertion, expected_client_id=CLIENT_ID, as_of=NOW)
            return "VERIFIED"
        except IdentityError:
            return "REJECTED"

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(attempt, range(16)))

    assert outcomes.count("VERIFIED") == 1
    assert outcomes.count("REJECTED") == 15


def test_sqlite_jti_replay_survives_restart_and_stores_only_hash(tmp_path: Path, rsa_keys) -> None:
    private_key, public_jwk = rsa_keys
    path = tmp_path / "assertion-replay.sqlite3"
    assertion = _assertion(private_key)
    first = PrivateKeyJwtVerifier((_policy(public_jwk),), SQLiteClientAssertionReplayStore(path))
    first.verify(assertion, expected_client_id=CLIENT_ID, as_of=NOW)

    restarted = PrivateKeyJwtVerifier(
        (_policy(public_jwk),), SQLiteClientAssertionReplayStore(path)
    )

    with pytest.raises(IdentityError, match="already been used"):
        restarted.verify(assertion, expected_client_id=CLIENT_ID, as_of=NOW)
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT jti_hash, client_id FROM client_assertion_replay"
        ).fetchone()
    assert row is not None
    assert len(row[0]) == 64
    assert row[0] != "A" * 22
    assert row[1] == CLIENT_ID
    assert assertion not in path.read_bytes().decode("utf-8", errors="ignore")


def test_private_key_jwt_errors_never_echo_assertion(rsa_keys) -> None:
    private_key, public_jwk = rsa_keys
    assertion = _assertion(private_key, claims={"aud": "wrong"})
    verifier = PrivateKeyJwtVerifier((_policy(public_jwk),), InMemoryClientAssertionReplayStore())

    with pytest.raises(IdentityError) as captured:
        verifier.verify(assertion, expected_client_id=CLIENT_ID, as_of=NOW)

    assert assertion not in str(captured.value)
