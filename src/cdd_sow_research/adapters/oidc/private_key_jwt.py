"""Pinned ``private_key_jwt`` client authentication with atomic replay control."""

from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ...domain.identity import IdentityError


class EndUserAuthUnavailableError(Exception):
    """The identity verifier cannot run because an operator dependency is absent.

    Distinct from :class:`~cdd_sow_research.domain.identity.IdentityError` on purpose: an
    ``IdentityError`` is a CALLER fault (a malformed or rejected token) and maps to HTTP
    401, whereas this is an ENVIRONMENT fault (the optional ``oidc`` extra, i.e. PyJWT, is
    not installed on the running service) and maps to HTTP 503. Reporting a missing PyJWT
    as a malformed token would tell a well-behaved caller their valid assertion is invalid;
    the two must not be conflated. The message names the missing extra so an operator can
    fix the deployment.
    """


_ALGORITHMS = frozenset({"RS256", "ES256"})
_FORBIDDEN_HEADERS = frozenset({"jku", "x5u", "jwk", "x5c"})
_JTI = re.compile(r"^[A-Za-z0-9._~-]{22,256}$")


@dataclass(frozen=True, slots=True)
class PinnedClientKey:
    kid: str
    algorithm: str
    public_jwk: dict[str, Any]

    def __post_init__(self) -> None:
        if not self.kid or len(self.kid) > 128:
            raise ValueError("private_key_jwt kid must be non-empty and bounded")
        if self.algorithm not in _ALGORITHMS:
            raise ValueError("private_key_jwt algorithm must be RS256 or ES256")
        if not isinstance(self.public_jwk, dict) or not self.public_jwk:
            raise ValueError("private_key_jwt public JWK must be configured")


@dataclass(frozen=True, slots=True)
class PrivateKeyJwtClientPolicy:
    client_id: str
    audience: str
    keys: tuple[PinnedClientKey, ...]
    max_lifetime_seconds: int = 60
    clock_skew_seconds: int = 30

    def __post_init__(self) -> None:
        if not self.client_id or len(self.client_id) > 256:
            raise ValueError("private_key_jwt client_id must be non-empty and bounded")
        if not self.audience or len(self.audience) > 512:
            raise ValueError("private_key_jwt audience must be non-empty and bounded")
        if not self.keys:
            raise ValueError("private_key_jwt requires at least one pinned key")
        identities = {(key.kid, key.algorithm) for key in self.keys}
        if len(identities) != len(self.keys):
            raise ValueError("private_key_jwt pinned key identities must be unique")
        if not 1 <= self.max_lifetime_seconds <= 60:
            raise ValueError("private_key_jwt lifetime must be from 1 to 60 seconds")
        if not 0 <= self.clock_skew_seconds <= 60:
            raise ValueError("private_key_jwt clock skew must be from 0 to 60 seconds")


@dataclass(frozen=True, slots=True)
class VerifiedBffClient:
    client_id: str
    assertion_correlation: str
    expires_at: datetime


@runtime_checkable
class ClientAssertionReplayPort(Protocol):
    def consume(
        self,
        *,
        jti: str,
        client_id: str,
        expires_at: datetime,
        as_of: datetime,
    ) -> str:
        """Consume one assertion JTI and return its safe correlation digest."""
        ...


class InMemoryClientAssertionReplayStore:
    """Thread-safe deterministic test adapter."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, datetime] = {}

    def consume(
        self,
        *,
        jti: str,
        client_id: str,
        expires_at: datetime,
        as_of: datetime,
    ) -> str:
        del client_id
        as_of = _utc(as_of)
        expires_at = _utc(expires_at)
        digest = _jti_digest(jti)
        with self._lock:
            self._entries = {key: expiry for key, expiry in self._entries.items() if expiry > as_of}
            if digest in self._entries:
                raise IdentityError("private_key_jwt assertion has already been used")
            self._entries[digest] = expires_at
        return digest


class SQLiteClientAssertionReplayStore:
    """Restart-safe local replay store; production wiring supplies a shared adapter."""

    def __init__(self, database_path: str | Path) -> None:
        path = Path(database_path)
        if str(path) in {"", ":memory:"}:
            raise ValueError("client assertion replay store requires a durable path")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._database_path = str(path)
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                PRAGMA synchronous = FULL;
                CREATE TABLE IF NOT EXISTS client_assertion_replay (
                    jti_hash TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    consumed_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS client_assertion_expiry
                    ON client_assertion_replay(expires_at);
                """
            )

    def consume(
        self,
        *,
        jti: str,
        client_id: str,
        expires_at: datetime,
        as_of: datetime,
    ) -> str:
        as_of = _utc(as_of)
        expires_at = _utc(expires_at)
        digest = _jti_digest(jti)
        try:
            with self._transaction() as connection:
                connection.execute(
                    "DELETE FROM client_assertion_replay WHERE expires_at <= ?",
                    (_timestamp(as_of),),
                )
                connection.execute(
                    """
                    INSERT INTO client_assertion_replay (
                        jti_hash, client_id, expires_at, consumed_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        digest,
                        client_id,
                        _timestamp(expires_at),
                        _timestamp(as_of),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise IdentityError("private_key_jwt assertion has already been used") from exc
        return digest

    @contextmanager
    def _transaction(self):
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, isolation_level=None, timeout=30)
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection


class PrivateKeyJwtVerifier:
    """Verify one assertion against an explicitly selected registered client."""

    def __init__(
        self,
        policies: tuple[PrivateKeyJwtClientPolicy, ...],
        replay_store: ClientAssertionReplayPort,
    ) -> None:
        self._policies = {policy.client_id: policy for policy in policies}
        if len(self._policies) != len(policies) or not self._policies:
            raise ValueError("private_key_jwt client policies must be non-empty and unique")
        self._replay_store = replay_store

    def verify(
        self,
        assertion: str,
        *,
        expected_client_id: str,
        as_of: datetime,
    ) -> VerifiedBffClient:
        policy = self._policies.get(expected_client_id)
        if policy is None:
            raise IdentityError("BFF client is not registered")
        header = _protected_header(assertion)
        if any(name in header for name in _FORBIDDEN_HEADERS):
            raise IdentityError("private_key_jwt contains a token-controlled key reference")
        if header.get("typ") not in (None, "JWT"):
            raise IdentityError("private_key_jwt protected typ must be JWT when present")
        algorithm = str(header.get("alg") or "")
        kid = str(header.get("kid") or "")
        key = next(
            (
                candidate
                for candidate in policy.keys
                if candidate.kid == kid and candidate.algorithm == algorithm
            ),
            None,
        )
        if key is None:
            raise IdentityError("private_key_jwt key or algorithm is not registered")
        claims = _verify_signature(assertion, key)
        _validate_claims(claims, policy=policy, as_of=as_of)
        jti = str(claims["jti"])
        expires_at = datetime.fromtimestamp(int(claims["exp"]), tz=UTC)
        correlation = self._replay_store.consume(
            jti=jti,
            client_id=policy.client_id,
            expires_at=expires_at + timedelta(seconds=policy.clock_skew_seconds),
            as_of=as_of,
        )
        return VerifiedBffClient(
            client_id=policy.client_id,
            assertion_correlation=correlation,
            expires_at=expires_at,
        )


def _protected_header(assertion: str) -> dict[str, Any]:
    if not isinstance(assertion, str) or assertion.count(".") != 2:
        raise IdentityError("private_key_jwt must be a compact signed JWT")
    jwt = _require_jwt()
    try:
        header: dict[str, Any] = jwt.get_unverified_header(assertion)
    except Exception as exc:  # noqa: BLE001
        raise IdentityError("private_key_jwt protected header is invalid") from exc
    return header


def _require_jwt() -> Any:
    """Import PyJWT, or raise the environment-fault error naming the missing extra.

    An absent PyJWT is an operator/deployment fault, never a caller fault: it must surface
    as :class:`EndUserAuthUnavailableError` (HTTP 503), distinct from the malformed-token
    :class:`IdentityError` (HTTP 401) a bad assertion earns once the library is present.
    """
    try:
        import jwt
    except ImportError as exc:
        raise EndUserAuthUnavailableError(
            "private_key_jwt verification is unavailable: the optional 'oidc' extra "
            "(PyJWT) is not installed on this service. Install it with "
            "'pip install cdd-sow-research[oidc]'."
        ) from exc
    return jwt


def _verify_signature(assertion: str, key: PinnedClientKey) -> dict[str, Any]:
    jwt = _require_jwt()
    try:
        public_key = jwt.PyJWK(key.public_jwk).key
        claims: dict[str, Any] = jwt.decode(
            assertion,
            public_key,
            algorithms=[key.algorithm],
            options={
                "require": ["iss", "sub", "aud", "iat", "exp", "jti"],
                "verify_iss": False,
                "verify_aud": False,
                "verify_iat": False,
                "verify_exp": False,
                "verify_nbf": False,
            },
        )
    except Exception as exc:  # noqa: BLE001
        raise IdentityError("private_key_jwt signature verification failed") from exc
    return claims


def _validate_claims(
    claims: dict[str, Any],
    *,
    policy: PrivateKeyJwtClientPolicy,
    as_of: datetime,
) -> None:
    as_of_timestamp = int(_utc(as_of).timestamp())
    if claims.get("iss") != policy.client_id or claims.get("sub") != policy.client_id:
        raise IdentityError("private_key_jwt iss and sub must equal the client_id")
    if claims.get("aud") != policy.audience:
        raise IdentityError("private_key_jwt audience is not the exact grant endpoint")
    issued_at = _numeric_date(claims, "iat")
    expires_at = _numeric_date(claims, "exp")
    if expires_at <= issued_at:
        raise IdentityError("private_key_jwt exp must be after iat")
    if expires_at - issued_at > policy.max_lifetime_seconds:
        raise IdentityError("private_key_jwt lifetime exceeds registered policy")
    if issued_at > as_of_timestamp + policy.clock_skew_seconds:
        raise IdentityError("private_key_jwt iat is in the future")
    if expires_at <= as_of_timestamp - policy.clock_skew_seconds:
        raise IdentityError("private_key_jwt has expired")
    if "nbf" in claims:
        not_before = _numeric_date(claims, "nbf")
        if not_before > as_of_timestamp + policy.clock_skew_seconds:
            raise IdentityError("private_key_jwt is not yet valid")
    jti = claims.get("jti")
    if not isinstance(jti, str) or _JTI.fullmatch(jti) is None:
        raise IdentityError("private_key_jwt jti must be a bounded opaque identifier")


def _numeric_date(claims: dict[str, Any], name: str) -> int:
    value = claims.get(name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise IdentityError(f"private_key_jwt {name} must be an integer NumericDate")
    return value


def _jti_digest(jti: str) -> str:
    if _JTI.fullmatch(jti) is None:
        raise IdentityError("private_key_jwt jti must be a bounded opaque identifier")
    return hashlib.sha256(jti.encode("ascii")).hexdigest()


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("time must be timezone-aware")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds")
