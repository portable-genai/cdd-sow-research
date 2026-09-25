"""Service-to-service (S2S) transport hardening shared by the platform adapters.

The ``platform`` profile's adapters are thin HTTP clients to the sibling
horizontal-platform and de-risking services. Two controls apply to every call:

* **Transport**: base URLs must be ``https://`` except for loopback development hosts
  (``localhost`` / ``127.0.0.1`` / ``::1``). A plaintext URL to a real host is a
  configuration error caught at adapter construction, not a silent downgrade. A base URL
  may carry a **path prefix**: a sibling reached through a shared authenticating edge is
  mounted under one, and :func:`validate_base_url` keeps it (stripping only a trailing
  slash) so a caller appending its own route lands inside that mount rather than at the
  host root.
* **Service identity**: when ``S2S_TOKEN`` is set, every request carries it as an
  ``Authorization: Bearer`` header (a Cloud Run ID token, an OIDC service-account JWT,
  or an API gateway key, per deployment). When ``S2S_SIGNING_KEY`` is set, the
  verified end-user actor is propagated as an HMAC-signed ``X-Cdd-Actor`` /
  ``X-Cdd-Actor-Sig`` header pair so the receiving service can authenticate the
  asserted user context instead of blindly trusting a JSON body field.

**The audience belongs to the receiver, not to the URL.** On a managed profile with no
``S2S_TOKEN`` this module mints a Google-signed ID token, and what the receiver accepts depends
on who the receiver is. A sibling Cloud Run service called directly accepts its own service
URL, which is the origin derived from the base URL here. A sibling reached through an
IAP-protected edge accepts only the **IAP OAuth client id**, and refuses a token minted for the
edge's origin or for the backend-service path IAP compares its own assertion against. A caller
in that position passes ``audience=`` explicitly, because nothing here can infer it from a URL.

**Sourced from the shared ``hex-service-kit`` commons.** The logic lives in
:mod:`hex_service_kit.s2s` rather than as a copy here; this module passes this repo's exact
env-var names (``S2S_*``) and header names (``X-Cdd-Actor`` / ``X-Cdd-Actor-Sig``) as
parameters. A fix to the S2S transport rule is a version bump of the package rather than an
N-repo edit.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from hex_service_kit.s2s import client_headers, validate_base_url

#: Env var holding the bearer credential for S2S calls. Three states, not two: ABSENT means
#: no header is attached (the offline zero-secret posture), a real value is sent as the bearer,
#: and PRESENT-BUT-EMPTY is an operator error that raises ``netdefaults.ConfiguredEmptyError``
#: from the commons rather than silently sending an unauthenticated request.
TOKEN_ENV = "S2S_TOKEN"
#: Env var holding the HMAC key for signing the propagated end-user actor.
SIGNING_KEY_ENV = "S2S_SIGNING_KEY"
#: This repo's header names for the signed-actor pair (kept stable for the receiving side).
_ACTOR_HEADER = "X-Cdd-Actor"
_ACTOR_SIG_HEADER = "X-Cdd-Actor-Sig"

# ``validate_base_url`` is re-exported verbatim from the commons (identical logic and error
# message); the callers import it from this module unchanged. It checks the scheme and the host
# and leaves any path prefix intact, which is what lets a caller name one app mounted behind a
# shared edge instead of a whole host.
__all__ = [
    "SIGNING_KEY_ENV",
    "TOKEN_ENV",
    "fetch_id_token",
    "headers",
    "service_origin",
    "validate_base_url",
]


def headers(
    *, settings: object, base_url: str, actor: str = "", audience: str = ""
) -> dict[str, str]:
    """Auth headers for one S2S request (bearer token + optional signed actor).

    Delegates to :func:`hex_service_kit.s2s.client_headers` with this repo's env-var and
    header names, so the output is byte-for-byte identical to the previous local copy.

    Args:
        audience: the audience the RECEIVER accepts for a minted ID token. Empty means a Cloud
            Run service called directly, so the audience is the origin of ``base_url``. A
            receiver behind an IAP edge accepts only the IAP OAuth client id, and the caller
            names it: see this module's docstring.
    """
    result = client_headers(
        actor,
        token_env=TOKEN_ENV,
        signing_key_env=SIGNING_KEY_ENV,
        actor_header=_ACTOR_HEADER,
        actor_sig_header=_ACTOR_SIG_HEADER,
    )
    managed = getattr(settings, "profile", "") in {"gcp", "platform"}
    if managed and base_url.startswith("https://") and "Authorization" not in result:
        result["Authorization"] = f"Bearer {fetch_id_token(audience or service_origin(base_url))}"
    return result


def service_origin(base_url: str) -> str:
    """The audience a Cloud Run service called DIRECTLY accepts: its origin, never a path."""
    parsed = urlsplit(base_url)
    return f"{parsed.scheme}://{parsed.netloc}"


def fetch_id_token(audience: str) -> str:
    """Mint a Google-signed ID token for ``audience`` with this process's workload identity.

    The one minting helper in this repo: the platform adapters use it through :func:`headers`,
    and the managed review router uses it as the per-submission bearer for a console behind the
    portal's IAP edge. The imports are lazy so the offline profiles never need ``google-auth``.
    """
    from google.auth.transport.requests import Request
    from google.oauth2 import id_token

    return str(id_token.fetch_id_token(Request(), audience))
