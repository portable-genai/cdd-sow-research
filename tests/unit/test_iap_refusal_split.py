"""The three refusals this repository's IAP adapter did not make, and the facts it copied.

This is the older of the two IAP adapter shapes in the fleet, and it was the ONE file in that
family shipping without this suite. Measured against ``adapters/gcp/identity.py``, which the
rest of the fleet carries, it was weaker in four ways, every one of them reachable with no
cloud SDK, no credential and no network. Three are refusals and are below; the fourth, the
``principals`` tuple, is closed in ``test_iap_claim_half.py`` where the whole claim half now
lives. Every test here was watched failing against the code exactly as it shipped.

**A configuration failure reported as an authentication failure.** An unconfigured audience
raised a plain ``IdentityError``, which ``api/security.py`` answers 401 "authentication
required". An operator reads that and goes looking for a missing credential; no credential
would have helped, because the deployment can authenticate nobody until a variable is set. It
was also checked SECOND, after the assertion header, so the deployment-level failure was
reported only to callers who already had an assertion: the operator probing with curl and no
header got "missing IAP assertion header" and learned nothing about the real problem.

**A whitespace-only header taking a different path.** ``ctx.header(...)`` was read without
``.strip()``. A header a proxy or a deployment template rendered blank is truthy, so it skipped
the missing-header refusal entirely and was refused further down by the algorithm pin, which
reports a malformed token for what is actually an absent one.

**A missing extra crashing rather than refusing.** The lazy google-auth import sat outside any
``try``. A deployment without the ``[gcp]`` extra raised ``ModuleNotFoundError`` out of
``resolve``, past ``get_authenticated_context``, and FastAPI answered a bare 500 on every
request: an empty error page for the caller and nothing to read for the operator.
"""

from __future__ import annotations

import base64
import builtins
import json as _json

import pytest
from hex_service_kit import federation as kit_federation

from cdd_sow_research.adapters.gcp.iap_identity import (
    IapAudienceUnconfiguredError,
    IapIdentityAdapter,
    IapVerifierUnavailableError,
)
from cdd_sow_research.domain.identity import IdentityError, RequestContext
from cdd_sow_research.ports.identity import EndUserAuthUnavailableError

_AUDIENCE = "/projects/1234567890/global/backendServices/42"


def _token(alg: str = "RS256") -> str:
    """A structurally real compact JWS. Only the header is read, and nothing is signed."""
    header = (
        base64.urlsafe_b64encode(_json.dumps({"alg": alg, "typ": "JWT"}).encode())
        .decode()
        .rstrip("=")
    )
    payload = base64.urlsafe_b64encode(b'{"sub":"1"}').decode().rstrip("=")
    return f"{header}.{payload}.c2ln"


def _adapter(audience: str = _AUDIENCE) -> IapIdentityAdapter:
    """The adapter with only its one piece of deployment configuration supplied.

    Built without touching ``Settings``: the audience is the single field ``resolve`` reads,
    and constructing the whole container would make these tests depend on every other port.
    """
    adapter = object.__new__(IapIdentityAdapter)
    adapter._settings = None
    adapter._audience = audience
    return adapter


# --------------------------------------------------------------------------------------- #
# The transport facts.
# --------------------------------------------------------------------------------------- #
def test_the_transport_facts_are_the_commons_values() -> None:
    """The header, the issuer and the key set are REBOUND from the kit, not re-declared.

    This module carried its own three literals until 2026-08-26, and so did
    ``api/security.py`` a few directories away, which is the same strings written twice inside
    one repository. Value equality is not enough to prove that is fixed, because a fresh copy
    satisfies it on the day it is written, so the SOURCE is asserted too.
    """
    import inspect

    from cdd_sow_research.adapters.gcp import iap_identity
    from cdd_sow_research.api import security

    assert iap_identity._ASSERTION_HEADER == kit_federation.IAP_ASSERTION_HEADER
    assert iap_identity._IAP_ISSUER == kit_federation.IAP_ISSUER
    assert iap_identity._IAP_KEYS_URL == kit_federation.IAP_KEYS_URL
    assert security._IAP_ASSERTION_HEADER == kit_federation.IAP_ASSERTION_HEADER
    assert security._PORTAL_ASSERTION_HEADER == kit_federation.PORTAL_ASSERTION_HEADER
    assert security._IAP_ISSUER == kit_federation.IAP_ISSUER
    assert security._IAP_KEYS_URL == kit_federation.IAP_KEYS_URL

    for module in (iap_identity, security):
        source = inspect.getsource(module)
        for literal in (
            kit_federation.IAP_ASSERTION_HEADER,
            kit_federation.PORTAL_ASSERTION_HEADER,
            kit_federation.IAP_ISSUER,
            kit_federation.IAP_KEYS_URL,
        ):
            assert f'"{literal}"' not in source, (
                f"{module.__name__} re-declares {literal} rather than rebinding it"
            )


# --------------------------------------------------------------------------------------- #
# (a) A configuration failure is the deployment's, not the caller's.
# --------------------------------------------------------------------------------------- #
def test_an_unconfigured_audience_is_a_deployment_failure_with_its_own_status() -> None:
    """503, not 401, and a type the API can tell apart from an ordinary refusal."""
    headers = {kit_federation.IAP_ASSERTION_HEADER: _token()}
    with pytest.raises(IapAudienceUnconfiguredError) as caught:
        _adapter(audience="").resolve(RequestContext(headers=headers))
    assert caught.value.http_status == 503
    assert isinstance(caught.value, EndUserAuthUnavailableError)
    assert isinstance(caught.value, IdentityError)
    # The message names the variable, because the fix is in the deployment.
    assert "CDD_IAP_AUDIENCE is not configured" in str(caught.value)


def test_the_audience_is_checked_before_the_header_so_a_bare_probe_learns_the_truth() -> None:
    """The ordering is the point, and it is what made this invisible.

    Checked second, an operator curling the service with no assertion header got "missing IAP
    assertion header" and went looking at the load balancer. The deployment-level failure was
    reported only to callers who already had an assertion, which is the population least
    likely to include the operator debugging it.
    """
    with pytest.raises(IapAudienceUnconfiguredError):
        _adapter(audience="").resolve(RequestContext(headers={}))


def test_a_configured_audience_still_refuses_a_caller_with_no_assertion_as_a_401() -> None:
    """The split must not swallow the ordinary case: no assertion is still the caller's problem."""
    with pytest.raises(IdentityError) as caught:
        _adapter().resolve(RequestContext(headers={}))
    assert not isinstance(caught.value, EndUserAuthUnavailableError)
    assert "missing IAP assertion header" in str(caught.value)


# --------------------------------------------------------------------------------------- #
# (b) A whitespace-only header is no header.
# --------------------------------------------------------------------------------------- #
@pytest.mark.parametrize("blank", ["   ", "\t", "\n", " \t\n "])
def test_a_whitespace_only_assertion_header_is_an_absent_one(blank: str) -> None:
    """Not "malformed token": absent. A blank value is truthy, so it took the other path."""
    headers = {kit_federation.IAP_ASSERTION_HEADER: blank}
    with pytest.raises(IdentityError) as caught:
        _adapter().resolve(RequestContext(headers=headers))
    assert "missing IAP assertion header" in str(caught.value)


# --------------------------------------------------------------------------------------- #
# (c) A missing extra refuses with a reason instead of crashing.
# --------------------------------------------------------------------------------------- #
def test_an_uninstalled_verifier_refuses_with_a_status_rather_than_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The import is blocked exactly as an environment without the [gcp] extra blocks it.

    Unwrapped this was a ``ModuleNotFoundError``, which is not an ``IdentityError``, so it
    escaped ``get_authenticated_context`` and became a bare 500 per request.
    """
    real_import = builtins.__import__

    def blocked(name: str, *args: object, **kwargs: object) -> object:
        if name.startswith("google"):
            raise ModuleNotFoundError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", blocked)
    with pytest.raises(IapVerifierUnavailableError) as caught:
        _adapter()._verify(_token())
    assert caught.value.http_status == 503
    assert isinstance(caught.value, EndUserAuthUnavailableError)
    assert "not installed" in str(caught.value)


# --------------------------------------------------------------------------------------- #
# The API answers the two differently, which is the only reason the split is worth having.
# --------------------------------------------------------------------------------------- #
def _request() -> object:
    from starlette.requests import Request

    return Request({"type": "http", "headers": [], "method": "GET", "path": "/"})


def test_the_api_answers_a_deployment_failure_with_its_own_status_and_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A type nothing reads is decoration. This is the read.

    The ``except`` ordering in ``get_authenticated_context`` is load-bearing:
    ``EndUserAuthUnavailableError`` is an ``IdentityError`` subclass, so the reverse order
    swallows it and answers the 401 the whole split exists to avoid.
    """
    from fastapi import HTTPException

    from cdd_sow_research.api import security

    class Unavailable:
        def authenticate(self, ctx: RequestContext, *, correlation: str = "") -> object:
            raise IapAudienceUnconfiguredError("CDD_IAP_AUDIENCE is not configured")

    monkeypatch.setattr(security, "get_authentication_port", lambda: Unavailable())
    with pytest.raises(HTTPException) as caught:
        security.get_authenticated_context(_request())
    assert caught.value.status_code == 503
    assert "CDD_IAP_AUDIENCE is not configured" in str(caught.value.detail)


def test_the_api_still_answers_an_ordinary_refusal_with_a_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """And an unauthenticated caller learns nothing they could use to forge the next attempt."""
    from fastapi import HTTPException

    from cdd_sow_research.api import security

    class Refusing:
        def authenticate(self, ctx: RequestContext, *, correlation: str = "") -> object:
            raise IdentityError("missing IAP assertion header; request did not pass through IAP")

    monkeypatch.setattr(security, "get_authentication_port", lambda: Refusing())
    with pytest.raises(HTTPException) as caught:
        security.get_authenticated_context(_request())
    assert caught.value.status_code == 401
