"""Mode 5 broker subject verification for the OIDC ID-token profile (Google).

A sibling of :class:`~cdd_sow_research.api.embed.Rfc9068BrokerSubjectTokenVerifier`, not a
relaxation of it. That verifier hard-rejects anything without protected header
``typ=at+jwt`` and must keep doing so: an ID token is not an API bearer. Google Cloud
Identity, however, is not a general-purpose authorization server for third-party APIs, so
the only credential a Google-only installation can present to the launch broker is an ID
token minted for a dedicated OAuth client. Section 7.3 of ``docs/embedding-and-identity.md``
already sanctions exactly that: a different external token type is accepted only through a
separately configured exchange policy that names the source type.

What this adapter pins, all from reviewed deployment policy and never from the token:

* exact ``iss`` (``https://accounts.google.com``);
* exact ``aud``, the dedicated OAuth client id used for nothing else;
* exact ``azp``, that same client, returned as the subject's authorised client;
* exact ``hd``, the reviewed Workspace hosted domain;
* the issued-at/expiry window, bounded by the reviewed maximum lifetime and skew.

The verified subject reports NO scopes. An ID token carries no ``scope`` claim, and the
broker grant scope comes from reviewed installation policy in ``api/embed.py``; returning
an empty tuple here makes it structurally impossible for this adapter to widen a grant.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

from ...api.embed import BrokerInstallationPolicy, VerifiedBrokerSubject
from ...config import IdTokenSubjectIssuerSettings
from ...domain.identity import IdentityError
from . import jwks_verify

_ALGORITHMS = frozenset({"RS256", "ES256"})
_FORBIDDEN_KEY_HEADERS = frozenset({"jku", "x5u", "jwk", "x5c"})
#: An RFC 9068 access token carries ``typ=at+jwt``. Accepting it here would defeat the
#: separation the two verifiers exist to enforce, so only the ID-token media types pass.
_PERMITTED_TYPES = frozenset({"JWT", "jwt", "application/jwt"})


class GoogleIdTokenBrokerSubjectVerifier:
    """Verify one Google ID token against an installation's reviewed ID-token policy."""

    def verify(
        self,
        token: str,
        *,
        installation: BrokerInstallationPolicy,
        as_of: datetime,
    ) -> VerifiedBrokerSubject:
        policy = installation.id_token_subject_policy
        if policy.audience != installation.subject_token_audience:
            raise IdentityError("ID-token policy audience does not match the installation")
        claims = _verified_claims(token, policy=policy)

        source_subject = _required_string(claims, "sub")
        authorized_party = _required_string(claims, policy.authorized_party_claim)
        hosted_domain = _required_string(claims, policy.hosted_domain_claim)
        if claims.get("aud") != policy.audience:
            raise IdentityError("ID token audience must be the exact reviewed client")
        if authorized_party != policy.authorized_party:
            raise IdentityError("ID token authorized party is not the reviewed client")
        if hosted_domain != policy.hosted_domain:
            raise IdentityError("ID token hosted domain is not the reviewed domain")

        issued_at = _numeric_date(claims, "iat")
        expires_at = _numeric_date(claims, "exp")
        if expires_at <= issued_at:
            raise IdentityError("ID token exp must be after iat")
        if expires_at - issued_at > policy.max_lifetime_seconds:
            raise IdentityError("ID token lifetime exceeds reviewed policy")
        issued = datetime.fromtimestamp(issued_at, tz=UTC)
        expires = datetime.fromtimestamp(expires_at, tz=UTC)
        skew = timedelta(seconds=policy.clock_skew_seconds)
        moment = _utc(as_of)
        if issued > moment + skew or expires <= moment - skew:
            raise IdentityError("ID token is outside its validity window")

        jti = str(claims.get("jti") or "").strip()
        return VerifiedBrokerSubject(
            issuer=policy.issuer,
            source_subject=source_subject,
            authorized_client=authorized_party,
            tenant=policy.tenant,
            # No scope claim exists; installation policy establishes the grant scope.
            scopes=(),
            # Google cannot sign a Doc1 installation claim. The installation binding comes
            # from the reviewed request installation plus the unique BFF/subject client
            # mapping enforced by ``StaticBrokerInstallationResolver``.
            signed_installation="",
            correlation=hashlib.sha256(jti.encode()).hexdigest() if jti else "",
            issued_at=issued,
            expires_at=expires,
        )


def _verified_claims(token: str, *, policy: IdTokenSubjectIssuerSettings) -> dict[str, Any]:
    header = _protected_header(token)
    if any(name in header for name in _FORBIDDEN_KEY_HEADERS):
        raise IdentityError("ID token contains a token-controlled key reference")
    token_type = header.get("typ")
    if token_type is not None and token_type not in _PERMITTED_TYPES:
        raise IdentityError("ID token protected typ must be a plain JWT when present")
    algorithm = str(header.get("alg") or "")
    if algorithm not in _ALGORITHMS or algorithm not in policy.algorithms:
        raise IdentityError("ID token protected algorithm is not allowed")
    kid = str(header.get("kid") or "").strip()
    if not kid:
        raise IdentityError("ID token protected header is missing kid")

    jwk = jwks_verify.signing_jwk(policy.jwks_uri, kid)
    try:
        import jwt

        signing_key = jwt.PyJWK(jwk).key
        claims: dict[str, Any] = jwt.decode(
            token,
            signing_key,
            algorithms=[algorithm],
            issuer=policy.issuer,
            audience=policy.audience,
            leeway=policy.clock_skew_seconds,
            options={
                "require": [
                    "iss",
                    "sub",
                    "aud",
                    "iat",
                    "exp",
                    policy.authorized_party_claim,
                    policy.hosted_domain_claim,
                ]
            },
        )
    except IdentityError:
        raise
    except Exception as exc:  # noqa: BLE001 - every verifier failure becomes a safe 401
        raise IdentityError(f"ID token verification failed: {exc}") from exc
    return claims


def _protected_header(token: str) -> dict[str, Any]:
    if not isinstance(token, str) or token.count(".") != 2:
        raise IdentityError("ID token must be a compact signed JWT")
    try:
        import jwt

        header: dict[str, Any] = jwt.get_unverified_header(token)
    except Exception as exc:  # noqa: BLE001 - malformed credentials fail closed
        raise IdentityError("ID token protected header is invalid") from exc
    return header


def _required_string(claims: dict[str, Any], name: str) -> str:
    value = claims.get(name)
    if not isinstance(value, str) or not value.strip():
        raise IdentityError(f"ID token claim {name!r} must be a non-empty string")
    return value.strip()


def _numeric_date(claims: dict[str, Any], name: str) -> int:
    value = claims.get(name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise IdentityError(f"ID token claim {name!r} must be an integer NumericDate")
    return value


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("ID token verification requires a timezone-aware moment")
    return value.astimezone(UTC)
