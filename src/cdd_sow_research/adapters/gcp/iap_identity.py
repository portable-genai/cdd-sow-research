"""GCP IdentityPort adapter: verify the Identity-Aware Proxy (IAP) signed assertion.

In secure mode the deployment is fronted by Cloud IAP (Cloud Run behind an HTTPS load
balancer + IAP), which authenticates the user against the configured IdP (Workspace, or an
external client IdP via Workforce Identity Federation) and injects a signed JWT in the
``x-goog-iap-jwt-assertion`` header. This adapter VERIFIES that assertion (signature,
audience, issuer, expiry) and derives the :class:`Principal` server-side, so authentication
is configured ON the GCP service rather than hand-rolled in the app. The Google SDK imports
are lazy (mirroring the other gcp adapters) so the SDK-free local/onprem profiles never
import them, and the verified assertion is never logged.
"""

from __future__ import annotations

from typing import Any

from hex_service_kit.assertion import require_claims, require_pinned_algorithm
from hex_service_kit.identity import IdentityError as AssertionRefused

from ...config import Settings
from ...domain.identity import IdentityError, Principal, RequestContext
from ...envread import optional_setting
from ...ports.identity import VERIFIED

_ASSERTION_HEADER = "x-goog-iap-jwt-assertion"
_IAP_KEYS_URL = "https://www.gstatic.com/iap/verify/public_key"

#: The issuer every IAP assertion carries. ``verify_token`` does not check the issuer at all
#: (``verify_oauth2_token`` is the wrapper that does), so this adapter checks it itself. The
#: docstring above claimed the issuer was verified long before anything verified it.
_IAP_ISSUER = "https://cloud.google.com/iap"

#: The claims this deployment requires before it reads any of them. ``email`` is here because it
#: is the subject the audit record attributes to; the previous ``email or sub`` reader accepted
#: an assertion carrying only one of them and could not tell an absent claim from an empty one.
_REQUIRED_CLAIMS = ("iss", "sub", "email", "exp")


class IapIdentityAdapter:
    """Verify the IAP-injected JWT assertion and derive a Principal (secure mode)."""

    #: Verifies a server-side assertion (signature, issuer, expiry, audience), so a caller
    #: cannot name itself. This declaration stands the exposure guard down, and it is only
    #: defensible because ``resolve`` refuses an unverifiable assertion.
    end_user_auth = VERIFIED

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        # Expected audience: the IAP-protected resource. For an HTTPS LB + IAP it is
        # "/projects/<NUM>/global/backendServices/<ID>"; for App Engine/Cloud Run IAP it is
        # "/projects/<NUM>/apps/<ID>". Configure via CDD_IAP_AUDIENCE; required in secure mode.
        self._audience = optional_setting("CDD_IAP_AUDIENCE") or ""

    def resolve(self, ctx: RequestContext) -> Principal:
        assertion = ctx.header(_ASSERTION_HEADER)
        if not assertion:
            raise IdentityError("missing IAP assertion header; request did not pass through IAP")
        if not self._audience:
            raise IdentityError("CDD_IAP_AUDIENCE is not configured; cannot verify IAP assertion")
        # The algorithm is judged BEFORE the verifier is handed the token, with no cryptography
        # and no cloud SDK, so the refusal is exercised by the offline gate rather than living
        # inside a library the gate does not install. `alg: none` is an unsigned assertion and
        # the HS* family would let a public key be used as an HMAC secret.
        self._refuse_unpinned_algorithm(assertion)
        claims = self._verify(assertion)
        # `verify_token` checks the signature, the audience and the expiry. It does NOT check the
        # issuer, so a Google-signed token from another issuer that satisfied the other two would
        # have been accepted here on the strength of a docstring that said otherwise.
        self._refuse_unpinned_claims(claims)
        subject = str(claims["email"]).strip()
        # Tenant from the hosted-domain claim; entitlement principals are derived
        # server-side (here, the verified subject; production maps Cloud Identity groups).
        tenant = str(claims.get("hd") or "").strip()
        principals: tuple[str, ...] = (f"user:{subject}",)
        return Principal(
            subject=subject,
            principals=principals,
            tenant=tenant,
            assurance="iap",
            source="gcp-iap",
        )

    def _refuse_unpinned_algorithm(self, assertion: str) -> None:
        """Refuse an assertion signed with an algorithm this deployment does not accept.

        The kit raises its own ``IdentityError``, which is NOT this repository's, so it is
        re-raised as the local one. Without that, the refusal would escape ``get_principal``
        and FastAPI would answer a bare 500 to a caller who should have been told 401.
        """
        try:
            require_pinned_algorithm(assertion)
        except AssertionRefused as exc:
            raise IdentityError(str(exc)) from exc

    def _refuse_unpinned_claims(self, claims: dict[str, Any]) -> None:
        """Refuse a verified assertion missing a required claim or naming the wrong party."""
        try:
            require_claims(
                claims,
                issuer=_IAP_ISSUER,
                audience=self._audience,
                required=_REQUIRED_CLAIMS,
            )
        except AssertionRefused as exc:
            raise IdentityError(str(exc)) from exc

    def _verify(self, assertion: str) -> dict[str, Any]:
        # Lazy import keeps the SDK-free profiles import-clean (mirrors the other gcp adapters).
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token

        try:
            # verify_token returns a Mapping; copy it into a dict so callers own a
            # mutable snapshot of the claims rather than the SDK's view of them.
            claims: dict[str, Any] = dict(
                id_token.verify_token(
                    assertion,
                    google_requests.Request(),
                    audience=self._audience,
                    certs_url=_IAP_KEYS_URL,
                )
            )
        except Exception as exc:  # noqa: BLE001 - any verification failure must become a 401
            raise IdentityError(f"IAP assertion verification failed: {exc}") from exc
        return claims
