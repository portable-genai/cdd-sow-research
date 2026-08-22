"""Service-to-service (S2S) transport hardening shared by the platform adapters.

The ``platform`` profile's adapters are thin HTTP clients to the sibling
horizontal-platform and de-risking services. Two controls apply to every call:

* **Transport**: base URLs must be ``https://`` except for loopback development hosts
  (``localhost`` / ``127.0.0.1`` / ``::1``). A plaintext URL to a real host is a
  configuration error caught at adapter construction, not a silent downgrade.
* **Service identity**: when ``HRZ_S2S_TOKEN`` is set, every request carries it as an
  ``Authorization: Bearer`` header (a Cloud Run ID token, an OIDC service-account JWT,
  or an API gateway key, per deployment). When ``HRZ_S2S_SIGNING_KEY`` is set, the
  verified end-user actor is propagated as an HMAC-signed ``X-Cdd-Actor`` /
  ``X-Cdd-Actor-Sig`` header pair so the receiving service can authenticate the
  asserted user context instead of blindly trusting a JSON body field.

**Sourced from the shared ``hex-service-kit`` commons.** The logic lives in
:mod:`hex_service_kit.s2s` rather than as a copy here; this module passes this repo's exact
env-var names (``HRZ_S2S_*``) and header names (``X-Cdd-Actor`` / ``X-Cdd-Actor-Sig``) as
parameters. A fix to the S2S transport rule is a version bump of the package rather than an
N-repo edit.
"""

from __future__ import annotations

from hex_service_kit.s2s import client_headers, validate_base_url

#: Env var holding the bearer credential for S2S calls. Three states, not two: ABSENT means
#: no header is attached (the offline zero-secret posture), a real value is sent as the bearer,
#: and PRESENT-BUT-EMPTY is an operator error that raises ``netdefaults.ConfiguredEmptyError``
#: from the commons rather than silently sending an unauthenticated request.
TOKEN_ENV = "HRZ_S2S_TOKEN"
#: Env var holding the HMAC key for signing the propagated end-user actor.
SIGNING_KEY_ENV = "HRZ_S2S_SIGNING_KEY"
#: This repo's header names for the signed-actor pair (kept stable for the receiving side).
_ACTOR_HEADER = "X-Cdd-Actor"
_ACTOR_SIG_HEADER = "X-Cdd-Actor-Sig"

# ``validate_base_url`` is re-exported verbatim from the commons (identical logic and error
# message); the callers import it from this module unchanged.
__all__ = ["SIGNING_KEY_ENV", "TOKEN_ENV", "headers", "validate_base_url"]


def headers(*, settings: object, base_url: str, actor: str = "") -> dict[str, str]:
    """Auth headers for one S2S request (bearer token + optional signed actor).

    Delegates to :func:`hex_service_kit.s2s.client_headers` with this repo's env-var and
    header names, so the output is byte-for-byte identical to the previous local copy.
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
        result["Authorization"] = f"Bearer {_fetch_id_token(base_url)}"
    return result


def _fetch_id_token(audience: str) -> str:
    from google.auth.transport.requests import Request
    from google.oauth2.id_token import fetch_id_token

    return fetch_id_token(Request(), audience)
