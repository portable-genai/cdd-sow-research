"""Settings-constructed identity adapter for exact embedded-grant resource tokens."""

from __future__ import annotations

from ...config import Settings
from ...domain.identity import IdentityError, Principal, RequestContext
from ...embedding.manifest import InstallationManifest
from ...ports.identity import VERIFIED
from .embed_token import EmbedTokenIssuer, EmbedTokenKey, EmbedTokenPolicy
from .embed_token_identity import (
    EmbeddedGrantTokenAuthenticationAdapter,
    VerifiedEmbeddedGrantIdentity,
)


def embed_token_issuer(settings: Settings) -> EmbedTokenIssuer:
    configured = settings.identity.embedded_grant.token
    if not configured.configured:
        raise ValueError("embedded-grant token policy is not configured")
    managed_signer = None
    if any(key.kms_key_version for key in configured.keys):
        from ..gcp.kms_embed_token import KmsEmbedTokenSigner

        managed_signer = KmsEmbedTokenSigner(settings)
    return EmbedTokenIssuer(
        EmbedTokenPolicy(
            issuer=configured.issuer,
            audience=configured.audience,
            active_kid=configured.active_kid,
            keys=tuple(
                EmbedTokenKey(
                    kid=key.kid,
                    algorithm=key.algorithm,
                    public_key_env=key.public_key_env,
                    private_key_env=key.private_key_env,
                    kms_key_version=key.kms_key_version,
                )
                for key in configured.keys
            ),
            lifetime_seconds=configured.lifetime_seconds,
            clock_skew_seconds=configured.clock_skew_seconds,
        ),
        managed_signer=managed_signer,
    )


class ConfiguredEmbeddedGrantAuthenticationAdapter:
    """Verify against the current reviewed manifest on every request."""

    #: Verifies a signed embedded-grant resource token against the reviewed manifest on
    #: every request, so a parent frame cannot assert an identity of its choosing.
    end_user_auth = VERIFIED

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._verifier = embed_token_issuer(settings)
        # Fail fast at construction, while deliberately retaining no authorization
        # snapshot: every request below reloads and revalidates these policy bytes.
        settings.installation_manifest()

    def _adapter(
        self,
    ) -> tuple[EmbeddedGrantTokenAuthenticationAdapter, InstallationManifest]:
        loaded = self._settings.installation_manifest()
        return (
            EmbeddedGrantTokenAuthenticationAdapter(
                self._verifier,
                installation_ids=tuple(
                    installation.installation_id for installation in loaded.manifest.installations
                ),
            ),
            loaded.manifest,
        )

    def resolve(self, ctx: RequestContext) -> Principal:
        return self.authenticate(ctx).principal

    def authenticate(self, ctx: RequestContext) -> VerifiedEmbeddedGrantIdentity:
        adapter, manifest = self._adapter()
        verified = adapter.authenticate(ctx)
        current = manifest.resolve(verified.verified_token.installation_id)
        token = verified.verified_token
        if token.tenant != current.tenant:
            raise IdentityError("embedded token tenant no longer matches installation policy")
        if token.client_id not in current.allowed_clients:
            raise IdentityError("embedded token client is no longer allowed by installation policy")
        if not token.scopes or not set(token.scopes).issubset(current.scopes):
            raise IdentityError(
                "embedded token scopes are no longer allowed by installation policy"
            )
        return verified
