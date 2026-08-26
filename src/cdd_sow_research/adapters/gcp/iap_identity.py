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
from hex_service_kit.federation import (
    IAP_ASSERTION_HEADER,
    IAP_ISSUER,
    IAP_KEYS_URL,
    FederationPolicy,
    principal_from_iap_claims,
)

from ...config import Settings
from ...domain.identity import IdentityError, Principal, RequestContext
from ...envread import optional_setting
from ...ports.identity import VERIFIED, EndUserAuthUnavailableError

# The three transport facts are REBOUND from the kit, not re-declared here. This module kept
# its own copies until 2026-08-26, which is a literal agreeing with itself and no way for the
# fleet to notice a divergence until two deployments disagreed about which header carries
# identity. ``api/security.py`` kept a second set of the same strings, which is the same
# defect twice inside one repository.
_ASSERTION_HEADER = IAP_ASSERTION_HEADER
_IAP_KEYS_URL = IAP_KEYS_URL

#: The issuer every IAP assertion carries. ``verify_token`` does not check the issuer at all
#: (``verify_oauth2_token`` is the wrapper that does), so this adapter checks it itself. The
#: docstring above claimed the issuer was verified long before anything verified it.
_IAP_ISSUER = IAP_ISSUER

#: The claims this deployment requires before it reads any of them. ``email`` is here because it
#: is the subject the audit record attributes to; the previous ``email or sub`` reader accepted
#: an assertion carrying only one of them and could not tell an absent claim from an empty one.
_REQUIRED_CLAIMS = ("iss", "sub", "email", "exp")

#: The reviewed policy the CLAIM half is evaluated under, and the whole of what this binding
#: decides about a verified caller once its signature has been checked.
#:
#: ``tenant_from_hosted_domain`` is ON, and it is an OPT-IN rather than a fallback. This
#: binding configures no domain map of its own, and IAP restricts the audience to one
#: organisation, so the ``hd`` claim IS the tenant id here. Left OFF, these same assertions
#: would resolve to no tenant at all, and ``api/security.py::IdentityPortAuthenticationAdapter``
#: refuses a non-local identity that resolved no tenant, so the binding would authenticate
#: nobody. Fail-closed and closed for everyone, and no offline gate would see it, because the
#: local profile never constructs this adapter.
#:
#: ``config.identity.iap_tenant_by_domain`` is a reviewed map this deployment CAN configure, and
#: it is deliberately not read here: it belongs to the other IAP implementation, the one on the
#: API request path, and wiring one reviewed map into two places that already disagree is the
#: defect ``tests/unit/test_iap_two_implementations.py`` exists to record rather than deepen.
_FEDERATION_POLICY = FederationPolicy(tenant_from_hosted_domain=True)

_VERIFIER_UNAVAILABLE = (
    "the IAP assertion verifier is not installed, so this deployment can authenticate nobody. "
    "Install the managed extra (pip install -r requirements-gcp.lock, or '.[gcp]') so "
    "google-auth is importable, or run a profile whose identity adapter needs no cloud SDK."
)

_UNCONFIGURED_AUDIENCE = (
    "CDD_IAP_AUDIENCE is not configured, so no IAP assertion can be verified and this "
    "deployment can authenticate nobody. Verifying WITHOUT an audience is not a fallback: "
    "google-auth documents audience=None as 'the audience is not verified', which would accept "
    "any Google-signed OIDC token from any project or application. Set it to the IAP-protected "
    "resource, /projects/<NUM>/global/backendServices/<ID>."
)


class IapAudienceUnconfiguredError(EndUserAuthUnavailableError):
    """No audience is configured, so nobody can be authenticated on this deployment.

    503 rather than 401: a caller who presented a perfectly good IAP assertion would be refused
    in exactly the same way, so inviting them to authenticate would be a lie. The message names
    the variable, because the fix is in the deployment and not in the request.
    """

    http_status = 503


class IapVerifierUnavailableError(EndUserAuthUnavailableError):
    """google-auth is not importable, so no assertion can be checked at all.

    Also 503, and for the same reason. This exists so the missing-SDK case is a refusal with a
    reason instead of the bare 500 an unwrapped ``ModuleNotFoundError`` produced: an
    uncredentialed caller got an empty error page and the operator got nothing to read.
    """

    http_status = 503


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
        # The configuration check comes FIRST, before the assertion header is even read. An
        # unconfigured audience is a deployment that can authenticate nobody, so refusing on
        # that alone means the refusal never depends on what the caller happened to present.
        # Checked second, as it was, the deployment failure was reported only to callers who
        # already had an assertion: the operator probing with curl and no header was told
        # "missing IAP assertion header" and went looking at the load balancer.
        if not self._audience:
            raise IapAudienceUnconfiguredError(_UNCONFIGURED_AUDIENCE)
        # Stripped, so a header a proxy or a deployment template rendered blank is ABSENT
        # rather than an assertion: a whitespace-only value is truthy, so it skipped this
        # refusal and was refused further down by the algorithm pin instead, which reports a
        # malformed token for what is actually a missing one.
        assertion = ctx.header(_ASSERTION_HEADER).strip()
        if not assertion:
            raise IdentityError("missing IAP assertion header; request did not pass through IAP")
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
        # Everything after the signature is ONE reviewed decision, and it is the commons
        # function rather than a fifty-first copy of it: which string is the subject, which
        # partition is the tenant, which entitlement principals the caller holds, what
        # assurance the audit record carries. The cryptography stays here, because the kit's
        # core is pure standard library with no runtime dependencies and verifies nothing.
        #
        # ``include_subject_principal`` is now STATED rather than assumed, which is what closes
        # the fourth gap this adapter carried: the tuple granted ``user:<subject>`` with nothing
        # saying whether that had been reviewed. It has been, it agrees with the IAP
        # implementation on the API request path, and ``tests/unit/test_iap_claim_half.py``
        # asserts it directly so a one-character edit cannot quietly change who holds what.
        return principal_from_iap_claims(
            claims,
            _FEDERATION_POLICY,
            source="gcp-iap",
            include_subject_principal=True,
        )

    def _refuse_unpinned_algorithm(self, assertion: str) -> None:
        """Refuse an assertion signed with an algorithm this deployment does not accept.

        The kit's refusal is already this repository's ``IdentityError``:
        ``domain/identity.py`` RE-EXPORTS the commons value rather than declaring a look-alike,
        so the two names are one class and the API maps it to a 401 unchanged. The comment here
        used to say the opposite and re-raised through a translating wrapper; that was true
        when it was written and had not been true since the re-export landed. The wrapper is
        gone. A stale claim about an exception boundary is exactly the sort of thing nothing
        notices, because both spellings behave identically until one of them does not.
        """
        require_pinned_algorithm(assertion)

    def _refuse_unpinned_claims(self, claims: dict[str, Any]) -> None:
        """Refuse a verified assertion missing a required claim or naming the wrong party."""
        require_claims(
            claims,
            issuer=_IAP_ISSUER,
            audience=self._audience,
            required=_REQUIRED_CLAIMS,
        )

    def _verify(self, assertion: str) -> dict[str, Any]:
        try:
            # Lazy import keeps the SDK-free profiles import-clean (mirrors the other gcp
            # adapters). Inside the try because an uninstalled verifier must refuse with a
            # reason and a status: unwrapped, the ModuleNotFoundError escaped resolve and
            # get_authenticated_context entirely and FastAPI answered a bare 500 on every
            # request, an empty error page for the caller and nothing to read for the operator.
            from google.auth.transport import requests as google_requests
            from google.oauth2 import id_token
        except ImportError as exc:
            raise IapVerifierUnavailableError(_VERIFIER_UNAVAILABLE) from exc

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
