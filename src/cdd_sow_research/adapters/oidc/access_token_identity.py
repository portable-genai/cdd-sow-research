"""Mode 4 RFC 9068 access-token authentication.

This verifier is deliberately distinct from the Mode 6 ID-token verifier. Every trust
input comes from reviewed deployment policy and the installation manifest, never from
token-selected URLs, audiences, tenants, or parent-supplied configuration.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any

from ...config import AccessTokenIssuerSettings, Settings
from ...domain.identity import IdentityError, Principal, RequestContext
from ...embedding.manifest import (
    Installation,
    LoadedInstallationManifest,
    VerifierPolicy,
    load_installation_manifest,
)
from ...ports.identity import VERIFIED
from . import jwks_verify

_ALGORITHMS = frozenset({"RS256", "ES256"})
_FORBIDDEN_KEY_HEADERS = frozenset({"jku", "x5u", "jwk", "x5c"})
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@dataclass(frozen=True, slots=True)
class VerifiedAccessTokenIdentity:
    """Verified Mode 4 result without the bearer token or raw jti."""

    principal: Principal
    issuer: str
    source_subject: str
    authorized_client: str
    effective_scopes: tuple[str, ...]
    installation_id: str
    correlation: str
    expires_at: int


@dataclass(frozen=True, slots=True)
class VerifiedRfc9068AccessToken:
    """Reusable external-token result for Mode 4 and Mode 5 subject policy."""

    issuer: str
    source_subject: str
    authorized_client: str
    tenant: str
    scopes: tuple[str, ...]
    groups: tuple[str, ...]
    signed_installation: str
    correlation: str
    issued_at: int
    expires_at: int


def verify_rfc9068_access_token(
    token: str,
    *,
    policy: AccessTokenIssuerSettings,
    audience: str | None = None,
    required_scopes: tuple[str, ...] = (),
) -> VerifiedRfc9068AccessToken:
    """Verify an external RFC 9068 token without applying a browser installation.

    Mode 5 may reuse this function with a distinct broker-audience policy. It must still
    apply its own BFF, subject, instance, and grant bindings. Mode 6 ID tokens and
    cdd-sow-research-issued embedded-grant tokens fail the exact issuer/audience/policy checks.
    """

    header = _protected_header(token)
    algorithm = str(header.get("alg") or "")
    if header.get("typ") != "at+jwt":
        raise IdentityError("access token protected typ must be exactly at+jwt")
    if algorithm not in _ALGORITHMS or algorithm not in policy.algorithms:
        raise IdentityError("access token protected algorithm is not allowed")
    if any(name in header for name in _FORBIDDEN_KEY_HEADERS):
        raise IdentityError("access token contains a token-controlled key reference")
    kid = str(header.get("kid") or "").strip()
    if not kid:
        raise IdentityError("access token protected header is missing kid")

    expected_audience = audience or policy.resource_audience
    jwk = jwks_verify.signing_jwk(policy.jwks_uri, kid)
    try:
        import jwt

        signing_key = jwt.PyJWK(jwk).key
        required = [
            "iss",
            "sub",
            "aud",
            "iat",
            "exp",
            policy.client_claim,
            policy.tenant_claim,
            policy.scope_claim,
        ]
        if policy.require_nbf:
            required.append("nbf")
        claims: dict[str, Any] = jwt.decode(
            token,
            signing_key,
            algorithms=[algorithm],
            issuer=policy.issuer,
            audience=expected_audience,
            leeway=policy.clock_skew_seconds,
            options={"require": required},
        )
    except IdentityError:
        raise
    except Exception as exc:  # noqa: BLE001 - all verifier failures become a safe 401
        raise IdentityError(f"access token verification failed: {exc}") from exc

    source_subject = _required_string(claims, "sub")
    client_id = _required_string(claims, policy.client_claim)
    tenant = _required_string(claims, policy.tenant_claim)
    if client_id not in policy.allowed_clients:
        raise IdentityError("access token authorized client is not allowed")
    if tenant != policy.tenant:
        raise IdentityError("access token tenant does not match issuer policy")
    if claims.get("aud") != expected_audience:
        raise IdentityError("access token audience must be the exact reviewed resource")
    scopes = _scopes(claims.get(policy.scope_claim))
    if not (set(policy.required_scopes) | set(required_scopes)).issubset(scopes):
        raise IdentityError("access token is missing required scopes")

    issued_at = _numeric_date(claims, "iat")
    expires_at = _numeric_date(claims, "exp")
    if expires_at <= issued_at:
        raise IdentityError("access token exp must be after iat")
    if expires_at - issued_at > policy.max_lifetime_seconds:
        raise IdentityError("access token lifetime exceeds reviewed policy")
    if issued_at > int(time.time()) + policy.clock_skew_seconds:
        raise IdentityError("access token iat is in the future")
    if "nbf" in claims:
        _ = _numeric_date(claims, "nbf")

    groups_claim = claims.get(policy.groups_claim, ())
    if isinstance(groups_claim, list) and all(
        isinstance(group, str) and group for group in groups_claim
    ):
        groups = tuple(groups_claim)
    elif groups_claim in (None, (), []):
        groups = ()
    else:
        raise IdentityError("access token groups claim must be a string array")
    jti = str(claims.get("jti") or "").strip()
    correlation = hashlib.sha256(jti.encode()).hexdigest() if jti else ""
    return VerifiedRfc9068AccessToken(
        issuer=policy.issuer,
        source_subject=source_subject,
        authorized_client=client_id,
        tenant=tenant,
        scopes=tuple(sorted(scopes)),
        groups=groups,
        signed_installation=str(claims.get(policy.installation_claim) or "").strip(),
        correlation=correlation,
        issued_at=issued_at,
        expires_at=expires_at,
    )


class OAuthAccessTokenAuthenticationAdapter:
    """Verify one cdd-sow-research-audience access token under exact installation policy."""

    #: Verifies an RFC 9068 access token against reviewed issuer policy and the
    #: installation manifest, never against token-selected trust inputs.
    end_user_auth = VERIFIED

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        policies = settings.identity.access_token_issuers
        if not policies:
            raise ValueError("oauth-access-token has no configured issuer policy")
        tenants = {policy.tenant for policy in policies}
        if len(tenants) != 1:
            raise ValueError("access-token policies in one deployment must share one tenant")
        self._policies = {policy.policy_id: policy for policy in policies}
        if len(self._policies) != len(policies):
            raise ValueError("access-token policy_id values must be unique")
        verifier_policies = tuple(
            VerifierPolicy(
                policy_id=policy.policy_id,
                enabled=True,
                identity_mode="oauth-access-token",
                credential_type="access-token",
                issuer=policy.issuer,
                resource_audience=policy.resource_audience,
                tenant=policy.tenant,
                allowed_clients=policy.allowed_clients,
                permitted_scopes=policy.required_scopes,
            )
            for policy in policies
        )
        self._manifest: LoadedInstallationManifest = load_installation_manifest(
            settings.channel.installation_manifest,
            expected_tenant=next(iter(tenants)),
            expected_identity_mode="oauth-access-token",
            verifier_policies=verifier_policies,
        )

    def resolve(self, ctx: RequestContext) -> Principal:
        """IdentityPort compatibility view."""
        return self.authenticate(ctx).principal

    def authenticate(self, ctx: RequestContext) -> VerifiedAccessTokenIdentity:
        installation = self._installation(ctx)
        policy = self._policies[installation.issuer_policy_id]
        token = _bearer_token(ctx)
        verified = verify_rfc9068_access_token(
            token,
            policy=policy,
            required_scopes=installation.scopes,
        )

        return self._verified_identity(
            verified,
            installation=installation,
            policy=policy,
        )

    def _installation(self, ctx: RequestContext) -> Installation:
        installation_id = ctx.header("x-cdd-installation-id").strip()
        if not installation_id:
            raise IdentityError("Mode 4 requires X-CDD-Installation-ID")
        try:
            installation = self._manifest.manifest.resolve(installation_id)
        except ValueError as exc:
            raise IdentityError("unknown Mode 4 installation") from exc
        origin = ctx.header("origin").strip()
        method = ctx.header(":method").upper()
        if origin and origin != installation.public_origin:
            raise IdentityError("request Origin does not match the dedicated agent origin")
        if method in _UNSAFE_METHODS and origin != installation.public_origin:
            raise IdentityError("unsafe Mode 4 request requires the exact agent Origin")
        return installation

    def _verified_identity(
        self,
        verified: VerifiedRfc9068AccessToken,
        *,
        installation: Installation,
        policy: AccessTokenIssuerSettings,
    ) -> VerifiedAccessTokenIdentity:
        if verified.authorized_client not in installation.allowed_clients:
            raise IdentityError("access token authorized client is not allowed")
        if verified.tenant != installation.tenant:
            raise IdentityError("access token tenant does not match installation policy")
        effective_scopes = tuple(sorted(set(verified.scopes) & set(installation.scopes)))

        reviewed_installation = policy.client_installations.get(verified.authorized_client, "")
        if verified.signed_installation:
            if verified.signed_installation != installation.installation_id:
                raise IdentityError("access token installation claim does not match")
            if reviewed_installation and reviewed_installation != verified.signed_installation:
                raise IdentityError("access token installation conflicts with reviewed mapping")
        elif reviewed_installation != installation.installation_id:
            raise IdentityError(
                "access token has neither a matching signed installation nor unique mapping"
            )

        actor = _canonical_actor(verified.issuer, verified.source_subject)
        principals = [f"user:{actor}"]
        principals.extend(f"group:{group}" for group in verified.groups)
        principal = Principal(
            subject=actor,
            principals=tuple(principals),
            tenant=verified.tenant,
            assurance="oauth-access-token",
            source="oauth-access-token",
        )
        return VerifiedAccessTokenIdentity(
            principal=principal,
            issuer=verified.issuer,
            source_subject=verified.source_subject,
            authorized_client=verified.authorized_client,
            effective_scopes=effective_scopes,
            installation_id=installation.installation_id,
            correlation=verified.correlation,
            expires_at=verified.expires_at,
        )


def _bearer_token(ctx: RequestContext) -> str:
    authorization = ctx.header("authorization").strip()
    scheme, separator, token = authorization.partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not token or " " in token:
        raise IdentityError("request requires exactly one Authorization Bearer token")
    if token.count(".") != 2:
        raise IdentityError("access token must be a compact signed JWT")
    return token


def _protected_header(token: str) -> dict[str, Any]:
    try:
        import jwt

        header: dict[str, Any] = jwt.get_unverified_header(token)
    except Exception as exc:  # noqa: BLE001 - malformed credentials fail closed
        raise IdentityError(f"could not read access token protected header: {exc}") from exc
    return header


def _required_string(claims: dict[str, Any], name: str) -> str:
    value = claims.get(name)
    if not isinstance(value, str) or not value.strip():
        raise IdentityError(f"access token claim {name!r} must be a non-empty string")
    return value.strip()


def _numeric_date(claims: dict[str, Any], name: str) -> int:
    value = claims.get(name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise IdentityError(f"access token claim {name!r} must be an integer NumericDate")
    return value


def _scopes(value: Any) -> set[str]:
    if isinstance(value, str):
        scopes = value.split()
    elif isinstance(value, list) and all(isinstance(scope, str) for scope in value):
        scopes = value
    else:
        raise IdentityError("access token scope claim must be a string or string array")
    if not scopes or any(not scope for scope in scopes):
        raise IdentityError("access token scopes must be non-empty")
    return set(scopes)


def _canonical_actor(issuer: str, source_subject: str) -> str:
    pair = json.dumps([issuer, source_subject], separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(pair).decode("ascii").rstrip("=")
    return f"issub:{encoded}"
