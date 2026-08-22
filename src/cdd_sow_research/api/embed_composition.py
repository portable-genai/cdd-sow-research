"""Composition root for the Mode 5 broker's explicit policy groups."""

from __future__ import annotations

from datetime import UTC, datetime

from ..adapters.oidc.configured_embed_identity import embed_token_issuer
from ..adapters.oidc.id_token_subject import GoogleIdTokenBrokerSubjectVerifier
from ..adapters.oidc.private_key_jwt import (
    ClientAssertionReplayPort,
    PinnedClientKey,
    PrivateKeyJwtClientPolicy,
    PrivateKeyJwtVerifier,
    SQLiteClientAssertionReplayStore,
)
from ..config import Container, Settings
from ..domain.browser_flow import ACCESS_TOKEN_SUBJECT_TYPE, ID_TOKEN_SUBJECT_TYPE
from .embed import (
    BffGrantClientPolicy,
    BrokerInstallationPolicy,
    BrokerSubjectTokenVerifier,
    EmbedBrokerDependencies,
    EmbedRateLimiter,
    InMemoryFixedWindowRateLimiter,
    Rfc9068BrokerSubjectTokenVerifier,
    StaticBrokerInstallationResolver,
    SubjectProfileRoutingVerifier,
)


def client_assertion_replay_store(settings: Settings) -> ClientAssertionReplayPort:
    """Select replay persistence explicitly; unsupported profiles fail closed."""
    profile = settings.profile
    if profile == "local":
        return SQLiteClientAssertionReplayStore(settings.local.client_assertion_replay_path)
    if profile in {"gcp", "platform"}:
        from ..adapters.gcp.firestore_browser_flow_store import (
            FirestoreClientAssertionReplayStore,
        )

        return FirestoreClientAssertionReplayStore(settings)
    raise NotImplementedError(
        f"profile {profile!r} has no reviewed shared client-assertion replay adapter"
    )


def embed_rate_limiter(settings: Settings) -> EmbedRateLimiter:
    """Use process-local limits only in local development."""
    configured = settings.browser_flow_store
    if settings.profile == "local":
        return InMemoryFixedWindowRateLimiter(
            max_attempts=configured.rate_limit_max_attempts,
            window_seconds=configured.rate_limit_window_seconds,
        )
    if settings.profile in {"gcp", "platform"}:
        from ..adapters.gcp.firestore_embed_rate_limiter import (
            FirestoreFixedWindowRateLimiter,
        )

        return FirestoreFixedWindowRateLimiter(
            settings,
            max_attempts=configured.rate_limit_max_attempts,
            window_seconds=configured.rate_limit_window_seconds,
        )
    raise NotImplementedError(
        f"profile {settings.profile!r} has no reviewed shared embed rate limiter"
    )


def _subject_token_verifier(
    installations: tuple[BrokerInstallationPolicy, ...],
) -> BrokerSubjectTokenVerifier:
    """Register exactly the subject profiles the reviewed installations actually accept."""
    configured = {installation.subject_token_type for installation in installations}
    available: dict[str, BrokerSubjectTokenVerifier] = {
        ACCESS_TOKEN_SUBJECT_TYPE: Rfc9068BrokerSubjectTokenVerifier(),
        ID_TOKEN_SUBJECT_TYPE: GoogleIdTokenBrokerSubjectVerifier(),
    }
    return SubjectProfileRoutingVerifier(
        {token_type: available[token_type] for token_type in sorted(configured)}
    )


def build_embed_broker_dependencies(container: Container) -> EmbedBrokerDependencies:
    """Build the broker only from startup-validated manifest and distinct policies."""
    settings = container.settings
    if settings.identity_mode != "embedded-grant":
        raise ValueError("Mode 5 broker composition requires exact embedded-grant identity")
    settings.validate_deployment()
    loaded = settings.installation_manifest()
    grant = settings.identity.embedded_grant
    configured = {policy.installation_id: policy for policy in grant.installations}
    installations: list[BrokerInstallationPolicy] = []
    bff_policies: list[PrivateKeyJwtClientPolicy] = []
    for installation in loaded.manifest.installations:
        policy = configured[installation.installation_id]
        subject_policy = grant.subject_policy(policy)
        bff_clients: list[BffGrantClientPolicy] = []
        for client in policy.bff_clients:
            bff_clients.append(
                BffGrantClientPolicy(
                    client_id=client.client_id,
                    permitted_scopes=client.permitted_scopes,
                    allowed_subject_clients=client.allowed_subject_clients,
                )
            )
            bff_policies.append(
                PrivateKeyJwtClientPolicy(
                    client_id=client.client_id,
                    audience=client.grant_endpoint_audience,
                    keys=tuple(
                        PinnedClientKey(
                            kid=key.kid,
                            algorithm=key.algorithm,
                            public_jwk=key.public_jwk,
                        )
                        for key in client.keys
                    ),
                    max_lifetime_seconds=client.max_lifetime_seconds,
                    clock_skew_seconds=client.clock_skew_seconds,
                )
            )
        installations.append(
            BrokerInstallationPolicy(
                installation_id=installation.installation_id,
                tenant=installation.tenant,
                protocol_versions=installation.protocol_versions,
                parent_origins=installation.parent_origins,
                permitted_scopes=installation.scopes,
                subject_grant_scope=policy.subject_grant_scope,
                subject_token_audience=policy.subject_token_audience,
                subject_token_policy=subject_policy,
                bff_clients=tuple(bff_clients),
                subject_token_type=policy.subject_token_type,
                registration_lifetime_seconds=policy.registration_lifetime_seconds,
            )
        )
    replay = client_assertion_replay_store(settings)
    return EmbedBrokerDependencies(
        store=container.browser_flow_store,
        installations=StaticBrokerInstallationResolver(tuple(installations)),
        subject_tokens=_subject_token_verifier(tuple(installations)),
        bff_assertions=PrivateKeyJwtVerifier(tuple(bff_policies), replay),
        token_issuer=embed_token_issuer(settings),
        rate_limiter=embed_rate_limiter(settings),
        clock=lambda: datetime.now(UTC),
    )
