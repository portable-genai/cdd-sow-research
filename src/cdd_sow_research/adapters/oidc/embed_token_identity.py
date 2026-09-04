"""Identity adapter for cdd-sow-research-issued embedded-grant resource tokens."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from ...domain.identity import IdentityError, Principal, RequestContext
from .embed_token import EmbedTokenIssuer, VerifiedEmbedToken


@dataclass(frozen=True, slots=True)
class VerifiedEmbeddedGrantIdentity:
    principal: Principal
    verified_token: VerifiedEmbedToken


class EmbeddedGrantTokenAuthenticationAdapter:
    """Accept only the dedicated embedded-grant token type for one installation."""

    def __init__(
        self,
        verifier: EmbedTokenIssuer,
        *,
        installation_ids: tuple[str, ...],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if (
            not installation_ids
            or len(set(installation_ids)) != len(installation_ids)
            or any(not installation_id for installation_id in installation_ids)
        ):
            raise ValueError("embedded-grant installation_ids must be non-empty and unique")
        self._verifier = verifier
        self._installation_ids = frozenset(installation_ids)
        self._clock = clock or (lambda: datetime.now(UTC))

    def resolve(self, ctx: RequestContext) -> Principal:
        return self.authenticate(ctx).principal

    def authenticate(
        self, ctx: RequestContext, *, as_of: datetime | None = None
    ) -> VerifiedEmbeddedGrantIdentity:
        authorization = ctx.header("authorization").strip()
        scheme, separator, token = authorization.partition(" ")
        if separator != " " or scheme.lower() != "bearer" or not token or " " in token:
            raise IdentityError("embedded-grant request requires one Bearer token")
        selected_installation = ctx.header("x-cdd-installation-id").strip()
        if selected_installation not in self._installation_ids:
            raise IdentityError("embedded-grant installation selector is not enabled")
        verified = self._verifier.verify(token, as_of=as_of or self._clock())
        if verified.installation_id != selected_installation:
            raise IdentityError("embedded token installation does not match selector")
        principal = Principal(
            subject=verified.subject,
            principals=(f"user:{verified.subject}",),
            tenant=verified.tenant,
            assurance="embedded-grant",
            source="embedded-grant",
        )
        return VerifiedEmbeddedGrantIdentity(
            principal=principal,
            verified_token=verified,
        )
