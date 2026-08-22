"""Dedicated asymmetric tokens issued after a Mode 5 grant is consumed."""

from __future__ import annotations

import base64
import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from ...domain.browser_flow import BrowserFlowState, EmbeddedGrantRecord
from ...domain.identity import IdentityError
from ...envread import optional_setting

_ALGORITHMS = frozenset({"ES256", "RS256"})
_FORBIDDEN_HEADERS = frozenset({"jku", "x5u", "jwk", "x5c"})


@dataclass(frozen=True, slots=True)
class EmbedTokenKey:
    kid: str
    algorithm: str
    public_key_env: str
    private_key_env: str = ""
    kms_key_version: str = ""

    def __post_init__(self) -> None:
        if not self.kid or len(self.kid) > 128:
            raise ValueError("embed-token kid must be non-empty and bounded")
        if self.algorithm not in _ALGORITHMS:
            raise ValueError("embed-token algorithm must be ES256 or RS256")
        if not self.public_key_env:
            raise ValueError("embed-token public key environment reference is required")
        if self.private_key_env and self.kms_key_version:
            raise ValueError("embed-token key has multiple signing sources")


@dataclass(frozen=True, slots=True)
class EmbedTokenPolicy:
    issuer: str
    audience: str
    active_kid: str
    keys: tuple[EmbedTokenKey, ...]
    lifetime_seconds: int = 300
    clock_skew_seconds: int = 30

    def __post_init__(self) -> None:
        if not self.issuer or not self.audience:
            raise ValueError("embed-token issuer and audience are required")
        identities = {(key.kid, key.algorithm) for key in self.keys}
        if not self.keys or len(identities) != len(self.keys):
            raise ValueError("embed-token accepted keys must be non-empty and unique")
        active = tuple(key for key in self.keys if key.kid == self.active_kid)
        if len(active) != 1 or not (active[0].private_key_env or active[0].kms_key_version):
            raise ValueError("embed-token active key requires one managed or local signer")
        if not 1 <= self.lifetime_seconds <= 300:
            raise ValueError("embed-token lifetime must be from 1 to 300 seconds")
        if not 0 <= self.clock_skew_seconds <= 30:
            raise ValueError("embed-token clock skew must be from 0 to 30 seconds")

    @property
    def active_key(self) -> EmbedTokenKey:
        return next(key for key in self.keys if key.kid == self.active_kid)


@dataclass(frozen=True, slots=True)
class MintedEmbedToken:
    access_token: str = field(repr=False)
    expires_at: datetime
    correlation: str


@dataclass(frozen=True, slots=True)
class VerifiedEmbedToken:
    subject: str
    source_issuer: str
    source_subject: str
    tenant: str
    installation_id: str
    client_id: str
    scopes: tuple[str, ...]
    issued_at: datetime
    expires_at: datetime
    correlation: str


class EmbedTokenIssuer:
    """Mint and verify the dedicated embedded-grant token type."""

    def __init__(
        self,
        policy: EmbedTokenPolicy,
        *,
        environment: Callable[[str], str | None] = optional_setting,
        token_id_factory: Callable[[], str] | None = None,
        managed_signer: Callable[[EmbedTokenKey, bytes], bytes] | None = None,
    ) -> None:
        self._policy = policy
        self._environment = environment
        self._token_id_factory = token_id_factory or (lambda: secrets.token_urlsafe(24))
        self._managed_signer = managed_signer

    def mint(self, record: EmbeddedGrantRecord, *, as_of: datetime) -> MintedEmbedToken:
        as_of = _utc(as_of)
        if record.state is not BrowserFlowState.CONSUMED or record.authorization is None:
            raise IdentityError("embedded grant must be consumed before token issuance")
        authorization = record.authorization
        subject_expiry = _utc(authorization.subject_expires_at)
        expires_at = min(
            as_of + timedelta(seconds=self._policy.lifetime_seconds),
            subject_expiry,
        )
        if expires_at <= as_of:
            raise IdentityError("upstream subject credential has expired")
        key = self._policy.active_key
        jti = self._token_id_factory()
        if not isinstance(jti, str) or len(jti) < 22 or len(jti) > 256:
            raise IdentityError("embedded-token identifier source is invalid")
        correlation = _correlation(jti)
        claims = {
            "iss": self._policy.issuer,
            "sub": canonical_actor(authorization.source_issuer, authorization.source_subject),
            "aud": self._policy.audience,
            "iat": int(as_of.timestamp()),
            "exp": int(expires_at.timestamp()),
            "jti": jti,
            "token_use": "doc1-embedded-grant",
            "source_iss": authorization.source_issuer,
            "source_sub": authorization.source_subject,
            "tenant": authorization.tenant,
            "installation_id": authorization.installation_id,
            "client_id": authorization.client_id,
            "scope": " ".join(sorted(authorization.scopes)),
        }
        try:
            if key.kms_key_version:
                if self._managed_signer is None:
                    raise IdentityError("managed embedded-token signer is unavailable")
                token = _encode_with_managed_signer(
                    claims,
                    key=key,
                    signer=self._managed_signer,
                )
            else:
                private_key = self._environment(key.private_key_env)
                if not private_key:
                    raise IdentityError("active embedded-token signing key is unavailable")
                import jwt

                token = jwt.encode(
                    claims,
                    private_key,
                    algorithm=key.algorithm,
                    headers={"kid": key.kid, "typ": "at+jwt"},
                )
        except IdentityError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise IdentityError("embedded-token signing failed") from exc
        return MintedEmbedToken(
            access_token=token,
            expires_at=expires_at,
            correlation=correlation,
        )

    def verify(self, token: str, *, as_of: datetime) -> VerifiedEmbedToken:
        as_of = _utc(as_of)
        header = _protected_header(token)
        if header.get("typ") != "at+jwt":
            raise IdentityError("embedded token protected typ must be exactly at+jwt")
        if any(name in header for name in _FORBIDDEN_HEADERS):
            raise IdentityError("embedded token contains a token-controlled key reference")
        kid = str(header.get("kid") or "")
        algorithm = str(header.get("alg") or "")
        key = next(
            (
                candidate
                for candidate in self._policy.keys
                if candidate.kid == kid and candidate.algorithm == algorithm
            ),
            None,
        )
        if key is None:
            raise IdentityError("embedded token key or algorithm is not accepted")
        public_key = self._environment(key.public_key_env)
        if not public_key:
            raise IdentityError("embedded-token verification key is unavailable")
        claims = _verify_signature(
            token,
            public_key=public_key,
            algorithm=key.algorithm,
        )
        return self._validated_claims(claims, as_of=as_of)

    def _validated_claims(self, claims: dict[str, Any], *, as_of: datetime) -> VerifiedEmbedToken:
        if claims.get("iss") != self._policy.issuer:
            raise IdentityError("embedded token issuer does not match")
        if claims.get("aud") != self._policy.audience:
            raise IdentityError("embedded token audience must be exact")
        if claims.get("token_use") != "doc1-embedded-grant":
            raise IdentityError("token is not a Doc1 embedded-grant resource token")
        source_issuer = _required_string(claims, "source_iss")
        source_subject = _required_string(claims, "source_sub")
        subject = _required_string(claims, "sub")
        if subject != canonical_actor(source_issuer, source_subject):
            raise IdentityError("embedded token subject provenance does not match")
        tenant = _required_string(claims, "tenant")
        installation_id = _required_string(claims, "installation_id")
        client_id = _required_string(claims, "client_id")
        scopes = _scopes(claims.get("scope"))
        issued_at_value = _numeric_date(claims, "iat")
        expires_at_value = _numeric_date(claims, "exp")
        issued_at = datetime.fromtimestamp(issued_at_value, tz=UTC)
        expires_at = datetime.fromtimestamp(expires_at_value, tz=UTC)
        if expires_at <= issued_at:
            raise IdentityError("embedded token exp must be after iat")
        if expires_at_value - issued_at_value > self._policy.lifetime_seconds:
            raise IdentityError("embedded token lifetime exceeds policy")
        skew = timedelta(seconds=self._policy.clock_skew_seconds)
        if issued_at > as_of + skew:
            raise IdentityError("embedded token iat is in the future")
        if expires_at <= as_of - skew:
            raise IdentityError("embedded token has expired")
        if "nbf" in claims:
            not_before = datetime.fromtimestamp(_numeric_date(claims, "nbf"), tz=UTC)
            if not_before > as_of + skew:
                raise IdentityError("embedded token is not yet valid")
        jti = _required_string(claims, "jti")
        return VerifiedEmbedToken(
            subject=subject,
            source_issuer=source_issuer,
            source_subject=source_subject,
            tenant=tenant,
            installation_id=installation_id,
            client_id=client_id,
            scopes=scopes,
            issued_at=issued_at,
            expires_at=expires_at,
            correlation=_correlation(jti),
        )


def canonical_actor(issuer: str, source_subject: str) -> str:
    if not issuer or not source_subject:
        raise ValueError("issuer and source subject are required")
    pair = json.dumps([issuer, source_subject], separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(pair).decode("ascii").rstrip("=")
    return f"issub:{encoded}"


def _protected_header(token: str) -> dict[str, Any]:
    if not isinstance(token, str) or token.count(".") != 2:
        raise IdentityError("embedded token must be a compact signed JWT")
    try:
        import jwt

        header: dict[str, Any] = jwt.get_unverified_header(token)
    except Exception as exc:  # noqa: BLE001
        raise IdentityError("embedded token protected header is invalid") from exc
    return header


def _encode_with_managed_signer(
    claims: dict[str, Any],
    *,
    key: EmbedTokenKey,
    signer: Callable[[EmbedTokenKey, bytes], bytes],
) -> str:
    header = {"alg": key.algorithm, "kid": key.kid, "typ": "at+jwt"}
    encoded_header = _base64url(json.dumps(header, sort_keys=True, separators=(",", ":")).encode())
    encoded_claims = _base64url(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode())
    signing_input = f"{encoded_header}.{encoded_claims}".encode("ascii")
    signature = signer(key, signing_input)
    if not isinstance(signature, bytes) or not signature:
        raise IdentityError("managed embedded-token signer returned no signature")
    return f"{encoded_header}.{encoded_claims}.{_base64url(signature)}"


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _verify_signature(
    token: str,
    *,
    public_key: str,
    algorithm: str,
) -> dict[str, Any]:
    try:
        import jwt

        claims: dict[str, Any] = jwt.decode(
            token,
            public_key,
            algorithms=[algorithm],
            options={
                "require": [
                    "iss",
                    "sub",
                    "aud",
                    "iat",
                    "exp",
                    "jti",
                    "token_use",
                    "source_iss",
                    "source_sub",
                    "tenant",
                    "installation_id",
                    "client_id",
                    "scope",
                ],
                "verify_iss": False,
                "verify_aud": False,
                "verify_iat": False,
                "verify_exp": False,
                "verify_nbf": False,
            },
        )
    except Exception as exc:  # noqa: BLE001
        raise IdentityError("embedded token signature verification failed") from exc
    return claims


def _required_string(claims: dict[str, Any], name: str) -> str:
    value = claims.get(name)
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise IdentityError(f"embedded token claim {name!r} must be non-empty and bounded")
    return value.strip()


def _numeric_date(claims: dict[str, Any], name: str) -> int:
    value = claims.get(name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise IdentityError(f"embedded token claim {name!r} must be an integer NumericDate")
    return value


def _scopes(value: Any) -> tuple[str, ...]:
    if not isinstance(value, str):
        raise IdentityError("embedded token scope must be a space-delimited string")
    scopes = value.split()
    if not scopes or any(not scope or len(scope) > 128 for scope in scopes):
        raise IdentityError("embedded token scopes must be non-empty and bounded")
    if len(set(scopes)) != len(scopes):
        raise IdentityError("embedded token scopes must not contain duplicates")
    return tuple(scopes)


def _correlation(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode()).hexdigest()


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("time must be timezone-aware")
    return value.astimezone(UTC)
