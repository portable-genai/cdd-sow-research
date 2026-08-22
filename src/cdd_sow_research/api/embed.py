"""Injectable Mode 5 broker routes and orchestration.

This module owns no global router or configuration.  The application composition root
must construct :class:`EmbedBrokerDependencies`, mount ``create_embed_router(...)``, and
provide reviewed installation, BFF, subject-token, rate-limit, store, and signing
implementations.
"""

from __future__ import annotations

import hashlib
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, Protocol, runtime_checkable

from fastapi import APIRouter, Header, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from ..adapters.oidc.access_token_identity import verify_rfc9068_access_token
from ..adapters.oidc.embed_token import MintedEmbedToken
from ..adapters.oidc.private_key_jwt import EndUserAuthUnavailableError, VerifiedBffClient
from ..config import AccessTokenIssuerSettings, IdTokenSubjectIssuerSettings
from ..domain.browser_flow import (
    ACCESS_TOKEN_SUBJECT_TYPE,
    ID_TOKEN_SUBJECT_TYPE,
    SUBJECT_TOKEN_TYPES,
    BrowserFlowBindingError,
    BrowserFlowExpiredError,
    BrowserFlowNotFoundError,
    BrowserFlowStateError,
    EmbeddedGrantRecord,
    GrantAuthorization,
    GrantFlowRegistration,
    IssuedGrantCode,
    RegisteredGrantInstance,
)
from ..domain.identity import IdentityError
from ..ports.browser_flow_store import BrowserFlowStorePort

_OPAQUE_BINDING = re.compile(r"^[A-Za-z0-9._~:-]{16,256}$")
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_CLIENT_ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"


@dataclass(frozen=True, slots=True)
class BffGrantClientPolicy:
    client_id: str
    permitted_scopes: tuple[str, ...]
    allowed_subject_clients: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.client_id or not self.permitted_scopes or not self.allowed_subject_clients:
            raise ValueError("BFF client policy fields must be non-empty")


@dataclass(frozen=True, slots=True)
class BrokerInstallationPolicy:
    installation_id: str
    tenant: str
    protocol_versions: tuple[str, ...]
    parent_origins: tuple[str, ...]
    permitted_scopes: tuple[str, ...]
    subject_grant_scope: str
    subject_token_audience: str
    subject_token_policy: AccessTokenIssuerSettings | IdTokenSubjectIssuerSettings
    bff_clients: tuple[BffGrantClientPolicy, ...]
    # The one RFC 8693 subject-token type this installation accepts. Reviewed
    # configuration, defaulting to the access-token profile: a request never selects it.
    subject_token_type: str = ACCESS_TOKEN_SUBJECT_TYPE
    registration_lifetime_seconds: int = 120

    def __post_init__(self) -> None:
        for name in (
            "installation_id",
            "tenant",
            "subject_grant_scope",
            "subject_token_audience",
        ):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"broker installation {name} must be non-empty")
        if (
            not self.protocol_versions
            or not self.parent_origins
            or not self.permitted_scopes
            or not self.bff_clients
        ):
            raise ValueError("broker installation policy collections must be non-empty")
        if self.subject_token_type not in SUBJECT_TOKEN_TYPES:
            raise ValueError("broker installation subject_token_type is not a reviewed token type")
        expected_policy = (
            AccessTokenIssuerSettings
            if self.subject_token_type == ACCESS_TOKEN_SUBJECT_TYPE
            else IdTokenSubjectIssuerSettings
        )
        if not isinstance(self.subject_token_policy, expected_policy):
            raise ValueError("subject-token policy shape must match the accepted token type")
        if not 1 <= self.registration_lifetime_seconds <= 120:
            raise ValueError("grant registration lifetime must be from 1 to 120 seconds")
        clients = {client.client_id for client in self.bff_clients}
        if len(clients) != len(self.bff_clients):
            raise ValueError("BFF clients must be unique within an installation")
        if self.subject_token_policy.tenant != self.tenant:
            raise ValueError("subject-token policy tenant must match installation")

    def client(self, client_id: str) -> BffGrantClientPolicy:
        matches = tuple(client for client in self.bff_clients if client.client_id == client_id)
        if len(matches) != 1:
            raise IdentityError("BFF client is not registered for installation")
        return matches[0]

    @property
    def access_token_subject_policy(self) -> AccessTokenIssuerSettings:
        """Narrow to the RFC 9068 profile, refusing an installation configured otherwise."""
        if not isinstance(self.subject_token_policy, AccessTokenIssuerSettings):
            raise IdentityError("installation does not accept an access-token subject")
        return self.subject_token_policy

    @property
    def id_token_subject_policy(self) -> IdTokenSubjectIssuerSettings:
        """Narrow to the ID-token profile, refusing an installation configured otherwise."""
        if not isinstance(self.subject_token_policy, IdTokenSubjectIssuerSettings):
            raise IdentityError("installation does not accept an ID-token subject")
        return self.subject_token_policy


@runtime_checkable
class BrokerInstallationResolver(Protocol):
    def resolve(self, installation_id: str) -> BrokerInstallationPolicy: ...


class StaticBrokerInstallationResolver:
    """Strict reviewed registry useful for tests and single-tenant deployment wiring."""

    def __init__(self, policies: tuple[BrokerInstallationPolicy, ...]) -> None:
        self._policies = {policy.installation_id: policy for policy in policies}
        if not self._policies or len(self._policies) != len(policies):
            raise ValueError("broker installation policies must be non-empty and unique")
        tenants = {policy.tenant for policy in policies}
        if len(tenants) != 1:
            raise ValueError("broker installation registry must contain exactly one tenant")
        client_installations: dict[str, str] = {}
        for policy in policies:
            for client in policy.bff_clients:
                for mapped_client in (
                    client.client_id,
                    *client.allowed_subject_clients,
                ):
                    previous = client_installations.setdefault(
                        mapped_client, policy.installation_id
                    )
                    if previous != policy.installation_id:
                        raise ValueError(
                            "each BFF/subject client must map to exactly one installation"
                        )

    def resolve(self, installation_id: str) -> BrokerInstallationPolicy:
        policy = self._policies.get(installation_id)
        if policy is None:
            raise BrowserFlowNotFoundError("installation is unknown")
        return policy


@dataclass(frozen=True, slots=True)
class VerifiedBrokerSubject:
    issuer: str
    source_subject: str
    authorized_client: str
    tenant: str
    scopes: tuple[str, ...]
    signed_installation: str
    correlation: str
    issued_at: datetime
    expires_at: datetime


@runtime_checkable
class BrokerSubjectTokenVerifier(Protocol):
    def verify(
        self,
        token: str,
        *,
        installation: BrokerInstallationPolicy,
        as_of: datetime,
    ) -> VerifiedBrokerSubject: ...


class Rfc9068BrokerSubjectTokenVerifier:
    """P4 adapter over P3's reusable RFC 9068 verification primitive."""

    def verify(
        self,
        token: str,
        *,
        installation: BrokerInstallationPolicy,
        as_of: datetime,
    ) -> VerifiedBrokerSubject:
        policy = installation.access_token_subject_policy
        verified = verify_rfc9068_access_token(
            token,
            policy=policy,
            audience=installation.subject_token_audience,
            required_scopes=(installation.subject_grant_scope,),
        )
        issued_at = datetime.fromtimestamp(verified.issued_at, tz=UTC)
        expires_at = datetime.fromtimestamp(verified.expires_at, tz=UTC)
        skew = timedelta(seconds=policy.clock_skew_seconds)
        if issued_at > _utc(as_of) + skew or expires_at <= _utc(as_of) - skew:
            raise IdentityError("broker subject token is outside its validity window")
        return VerifiedBrokerSubject(
            issuer=verified.issuer,
            source_subject=verified.source_subject,
            authorized_client=verified.authorized_client,
            tenant=verified.tenant,
            scopes=verified.scopes,
            signed_installation=verified.signed_installation,
            correlation=verified.correlation,
            issued_at=issued_at,
            expires_at=expires_at,
        )


class SubjectProfileRoutingVerifier:
    """Route to the verifier registered for the installation's reviewed subject profile.

    The composition root registers one verifier per accepted token type. An installation
    whose configured type has no registered verifier is refused, so a deployment can never
    silently fall back to a profile it did not review.
    """

    def __init__(self, verifiers: dict[str, BrokerSubjectTokenVerifier]) -> None:
        unknown = set(verifiers) - set(SUBJECT_TOKEN_TYPES)
        if not verifiers or unknown:
            raise ValueError("subject verifiers must be keyed by reviewed subject token types")
        self._verifiers = dict(verifiers)

    def verify(
        self,
        token: str,
        *,
        installation: BrokerInstallationPolicy,
        as_of: datetime,
    ) -> VerifiedBrokerSubject:
        verifier = self._verifiers.get(installation.subject_token_type)
        if verifier is None:
            raise IdentityError("no reviewed verifier for the installation subject token type")
        return verifier.verify(token, installation=installation, as_of=as_of)


@runtime_checkable
class BffClientAssertionVerifier(Protocol):
    def verify(
        self,
        assertion: str,
        *,
        expected_client_id: str,
        as_of: datetime,
    ) -> VerifiedBffClient: ...


@runtime_checkable
class EmbedAccessTokenIssuer(Protocol):
    def mint(self, record: EmbeddedGrantRecord, *, as_of: datetime) -> MintedEmbedToken: ...


class RateLimitExceeded(RuntimeError):
    pass


@runtime_checkable
class EmbedRateLimiter(Protocol):
    def check(self, operation: str, key: str, *, as_of: datetime) -> None: ...


class InMemoryFixedWindowRateLimiter:
    """Thread-safe local hook; production wiring must provide shared enforcement."""

    def __init__(self, *, max_attempts: int = 20, window_seconds: int = 60) -> None:
        if max_attempts < 1 or window_seconds < 1:
            raise ValueError("rate-limit values must be positive")
        self._max_attempts = max_attempts
        self._window = timedelta(seconds=window_seconds)
        self._entries: dict[tuple[str, str], tuple[datetime, int]] = {}
        self._lock = threading.Lock()

    def check(self, operation: str, key: str, *, as_of: datetime) -> None:
        as_of = _utc(as_of)
        safe_key = hashlib.sha256(key.encode()).hexdigest()
        bucket = (operation, safe_key)
        with self._lock:
            started_at, count = self._entries.get(bucket, (as_of, 0))
            if as_of >= started_at + self._window:
                started_at, count = as_of, 0
            if count >= self._max_attempts:
                raise RateLimitExceeded("embed broker rate limit exceeded")
            self._entries[bucket] = (started_at, count + 1)


class HostProofError(RuntimeError):
    pass


class HostAuthorizationProof(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    host_origin: str = Field(min_length=1, max_length=512)
    fetch_site: str = Field(min_length=1, max_length=32)
    csrf_verified: bool
    session_binding: str = Field(min_length=1, max_length=256)
    session_source_subject: str = Field(min_length=1, max_length=512)
    user_intent_id: str = Field(min_length=1, max_length=256)


class RegisterInstanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    installation_id: str = Field(min_length=1, max_length=128)
    protocol_version: str = Field(min_length=1, max_length=16)
    pkce_challenge: str = Field(min_length=43, max_length=43)
    pkce_method: Literal["S256"]


class RegisterInstanceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    instance_id: str
    state: Literal["REGISTERED"]
    expires_at: int


class GrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    installation_id: str = Field(min_length=1, max_length=128)
    instance_id: str = Field(min_length=22, max_length=256)
    client_id: str = Field(min_length=1, max_length=256)
    client_assertion_type: Literal["urn:ietf:params:oauth:client-assertion-type:jwt-bearer"]
    client_assertion: str = Field(min_length=1, max_length=16384)
    # The schema admits both reviewed RFC 8693 subject types; which one an installation
    # actually accepts is decided by its policy in ``EmbedBrokerService.authorize``.
    subject_token_type: Literal[
        "urn:ietf:params:oauth:token-type:access_token",
        "urn:ietf:params:oauth:token-type:id_token",
    ]
    subject_token: str = Field(min_length=1, max_length=16384)
    requested_scopes: tuple[str, ...] = Field(min_length=1, max_length=32)
    host_proof: HostAuthorizationProof


class GrantResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    launch_code: str
    state: Literal["CODE_ISSUED"]
    expires_at: int


class RedeemTokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    installation_id: str = Field(min_length=1, max_length=128)
    instance_id: str = Field(min_length=22, max_length=256)
    launch_code: str = Field(min_length=22, max_length=256)
    pkce_verifier: str = Field(min_length=43, max_length=128)


class RedeemTokenResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    access_token: str
    token_type: Literal["Bearer"]
    expires_in: int
    scope: str


@dataclass(frozen=True, slots=True)
class EmbedBrokerDependencies:
    store: BrowserFlowStorePort
    installations: BrokerInstallationResolver
    subject_tokens: BrokerSubjectTokenVerifier
    bff_assertions: BffClientAssertionVerifier
    token_issuer: EmbedAccessTokenIssuer
    rate_limiter: EmbedRateLimiter
    clock: Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class RedeemedGrantToken:
    token: MintedEmbedToken
    scopes: tuple[str, ...]


class EmbedBrokerService:
    def __init__(self, dependencies: EmbedBrokerDependencies) -> None:
        self._dependencies = dependencies

    def register(
        self, request: RegisterInstanceRequest, *, correlation_id: str
    ) -> RegisteredGrantInstance:
        as_of = _utc(self._dependencies.clock())
        policy = self._dependencies.installations.resolve(request.installation_id)
        self._dependencies.rate_limiter.check("register", request.installation_id, as_of=as_of)
        if request.protocol_version not in policy.protocol_versions:
            raise ValueError("embed protocol version is not enabled")
        registration = GrantFlowRegistration(
            installation_id=policy.installation_id,
            tenant=policy.tenant,
            protocol_version=request.protocol_version,
            pkce_challenge=request.pkce_challenge,
            pkce_method=request.pkce_method,
            correlation_id=correlation_id,
        )
        return self._dependencies.store.register_grant(
            registration,
            now=as_of,
            expires_at=as_of + timedelta(seconds=policy.registration_lifetime_seconds),
        )

    def authorize(self, request: GrantRequest) -> IssuedGrantCode:
        as_of = _utc(self._dependencies.clock())
        policy = self._dependencies.installations.resolve(request.installation_id)
        client_policy = policy.client(request.client_id)
        self._dependencies.rate_limiter.check(
            "authorize", f"{request.installation_id}:{request.client_id}", as_of=as_of
        )
        if request.client_assertion_type != _CLIENT_ASSERTION_TYPE:
            raise IdentityError("unsupported client assertion type")
        bff = self._dependencies.bff_assertions.verify(
            request.client_assertion,
            expected_client_id=client_policy.client_id,
            as_of=as_of,
        )
        if bff.client_id != client_policy.client_id:
            raise IdentityError("BFF assertion client does not match")
        if request.subject_token_type != policy.subject_token_type:
            raise IdentityError("subject token type is not accepted by this installation")
        subject = self._dependencies.subject_tokens.verify(
            request.subject_token,
            installation=policy,
            as_of=as_of,
        )
        subject_scopes = self._subject_scopes(subject, policy=policy)
        self._validate_subject(
            subject,
            policy=policy,
            client=client_policy,
            subject_scopes=subject_scopes,
        )
        self._validate_host_proof(request.host_proof, policy=policy, subject=subject)
        requested = _scope_set(request.requested_scopes)
        effective = tuple(
            sorted(
                requested
                & set(subject_scopes)
                & set(policy.permitted_scopes)
                & set(client_policy.permitted_scopes)
            )
        )
        if not effective:
            raise IdentityError("grant scope intersection is empty")
        authorization = GrantAuthorization(
            installation_id=policy.installation_id,
            client_id=bff.client_id,
            source_issuer=subject.issuer,
            source_subject=subject.source_subject,
            tenant=subject.tenant,
            scopes=effective,
            subject_expires_at=subject.expires_at,
        )
        return self._dependencies.store.authorize_grant(
            request.instance_id,
            authorization,
            as_of=as_of,
        )

    def redeem(self, request: RedeemTokenRequest) -> RedeemedGrantToken:
        as_of = _utc(self._dependencies.clock())
        policy = self._dependencies.installations.resolve(request.installation_id)
        self._dependencies.rate_limiter.check(
            "redeem", f"{policy.installation_id}:{request.instance_id}", as_of=as_of
        )
        consumed = self._dependencies.store.consume_grant(
            request.instance_id,
            request.launch_code,
            request.pkce_verifier,
            installation_id=policy.installation_id,
            as_of=as_of,
        )
        if consumed.authorization is None:
            raise BrowserFlowStateError("consumed grant authorization is unavailable")
        return RedeemedGrantToken(
            token=self._dependencies.token_issuer.mint(consumed, as_of=as_of),
            scopes=consumed.authorization.scopes,
        )

    @staticmethod
    def _subject_scopes(
        subject: VerifiedBrokerSubject,
        *,
        policy: BrokerInstallationPolicy,
    ) -> tuple[str, ...]:
        """The scopes the verified subject credential establishes for this installation.

        Under the access-token profile that is the token's own ``scope`` claim, and it
        stays the ceiling. An OIDC ID token has NO scope claim, so under that profile the
        scopes come from reviewed installation policy plus the verified issuer/audience/
        authorised-party/hosted-domain tuple: the grant scope the installation was
        registered with, and the resource scopes the manifest already permits it. The
        host's requested scopes are still intersected with the installation's and the
        BFF client's permitted sets below, so this derivation can never widen a grant
        beyond what was reviewed, and a scope asserted by the host is never sufficient.
        """
        if policy.subject_token_type != ID_TOKEN_SUBJECT_TYPE:
            return subject.scopes
        return tuple(sorted({policy.subject_grant_scope, *policy.permitted_scopes}))

    @staticmethod
    def _validate_subject(
        subject: VerifiedBrokerSubject,
        *,
        policy: BrokerInstallationPolicy,
        client: BffGrantClientPolicy,
        subject_scopes: tuple[str, ...],
    ) -> None:
        if subject.tenant != policy.tenant:
            raise IdentityError("subject token tenant does not match installation")
        if subject.authorized_client not in client.allowed_subject_clients:
            raise IdentityError("subject token client is not allowed for BFF")
        if subject.signed_installation and subject.signed_installation != policy.installation_id:
            raise IdentityError("subject token installation does not match")
        if policy.subject_grant_scope not in subject_scopes:
            raise IdentityError("subject token lacks the broker grant scope")

    @staticmethod
    def _validate_host_proof(
        proof: HostAuthorizationProof,
        *,
        policy: BrokerInstallationPolicy,
        subject: VerifiedBrokerSubject,
    ) -> None:
        if proof.host_origin not in policy.parent_origins:
            raise HostProofError("host Origin does not match installation")
        if proof.fetch_site != "same-origin" or proof.csrf_verified is not True:
            raise HostProofError("host session controls were not verified")
        if proof.session_source_subject != subject.source_subject:
            raise HostProofError("host session subject does not match broker subject")
        if _SHA256_HEX.fullmatch(proof.session_binding) is None:
            raise HostProofError("host session binding must be a SHA-256 correlation")
        if _OPAQUE_BINDING.fullmatch(proof.user_intent_id) is None:
            raise HostProofError("host user intent binding is invalid")


class _NoStoreEmbedRoute(APIRoute):
    """Apply no-store and suppress validation echoes for secret-bearing bodies."""

    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request: Request):
            try:
                response = await original(request)
            except RequestValidationError:
                return JSONResponse(
                    {"detail": "embed request rejected"},
                    status_code=status.HTTP_400_BAD_REQUEST,
                    headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
                )
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
            return response

        return handler


def create_embed_router(dependencies: EmbedBrokerDependencies) -> APIRouter:
    """Create a router with no hidden global configuration or dependency state."""

    service = EmbedBrokerService(dependencies)
    router = APIRouter(
        prefix="/v1/embed",
        tags=["embed"],
        route_class=_NoStoreEmbedRoute,
    )

    @router.post(
        "/instances",
        response_model=RegisterInstanceResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def register_instance(
        request: RegisterInstanceRequest,
        response: Response,
        x_request_id: Annotated[str, Header(max_length=128)] = "",
    ) -> RegisterInstanceResponse:
        _no_store(response)
        correlation = (
            x_request_id
            if _OPAQUE_BINDING.fullmatch(x_request_id)
            else hashlib.sha256(
                f"{request.installation_id}:{request.pkce_challenge}".encode()
            ).hexdigest()
        )
        try:
            registered = service.register(request, correlation_id=correlation)
        except Exception as exc:
            raise _http_error(exc) from exc
        return RegisterInstanceResponse(
            instance_id=registered.opaque_instance_id,
            state="REGISTERED",
            expires_at=int(registered.record.expires_at.timestamp()),
        )

    @router.post("/grants", response_model=GrantResponse)
    def issue_grant(request: GrantRequest, response: Response) -> GrantResponse:
        _no_store(response)
        try:
            issued = service.authorize(request)
        except Exception as exc:
            raise _http_error(exc) from exc
        assert issued.record.code_expires_at is not None
        return GrantResponse(
            launch_code=issued.launch_code,
            state="CODE_ISSUED",
            expires_at=int(issued.record.code_expires_at.timestamp()),
        )

    @router.post("/token", response_model=RedeemTokenResponse)
    def redeem_token(request: RedeemTokenRequest, response: Response) -> RedeemTokenResponse:
        _no_store(response)
        try:
            redeemed = service.redeem(request)
        except Exception as exc:
            raise _http_error(exc) from exc
        return RedeemTokenResponse(
            access_token=redeemed.token.access_token,
            token_type="Bearer",
            expires_in=max(
                0,
                int((redeemed.token.expires_at - _utc(dependencies.clock())).total_seconds()),
            ),
            scope=" ".join(redeemed.scopes),
        )

    return router


def _scope_set(scopes: tuple[str, ...]) -> set[str]:
    if not scopes:
        raise ValueError("requested scopes must be non-empty")
    result = set(scopes)
    if len(result) != len(scopes) or any(
        not scope or len(scope) > 128 or any(character.isspace() for character in scope)
        for scope in scopes
    ):
        raise ValueError("requested scopes must be unique bounded tokens")
    return result


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _http_error(exc: Exception) -> HTTPException:
    headers = {"Cache-Control": "no-store", "Pragma": "no-cache"}
    if isinstance(exc, RateLimitExceeded):
        return HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "embed broker rate limited",
            headers=headers,
        )
    if isinstance(exc, EndUserAuthUnavailableError):
        # An operator/environment fault (the oidc extra is not installed), NOT a caller
        # fault: a well-formed assertion must not be rejected as unauthorized because the
        # service cannot verify it. 503 names the missing capability so it is fixable.
        return HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "embed identity verification unavailable: the oidc extra is not installed",
            headers=headers,
        )
    if isinstance(exc, BrowserFlowExpiredError):
        return HTTPException(status.HTTP_410_GONE, "embed grant expired", headers=headers)
    if isinstance(exc, BrowserFlowStateError):
        return HTTPException(
            status.HTTP_409_CONFLICT,
            "embed grant state rejected",
            headers=headers,
        )
    if isinstance(
        exc,
        (
            IdentityError,
            BrowserFlowBindingError,
            BrowserFlowNotFoundError,
        ),
    ):
        return HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "embed authorization rejected",
            headers=headers,
        )
    if isinstance(exc, HostProofError):
        return HTTPException(
            status.HTTP_403_FORBIDDEN,
            "host authorization proof rejected",
            headers=headers,
        )
    if isinstance(exc, ValueError):
        return HTTPException(status.HTTP_400_BAD_REQUEST, "embed request rejected", headers=headers)
    return HTTPException(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "embed broker unavailable",
        headers=headers,
    )


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("broker clock must return a timezone-aware datetime")
    return value.astimezone(UTC)
