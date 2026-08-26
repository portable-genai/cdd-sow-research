"""This repository had TWO IAP claim halves, and they disagreed. Now there is one.

``config/settings.yaml`` binds ``identity.bindings.iap`` to
:class:`~cdd_sow_research.adapters.gcp.iap_identity.IapIdentityAdapter`, so that adapter is what
``container.identity`` holds, and its ``end_user_auth = VERIFIED`` class attribute is what
stands the exposure guard down. But ``api/security.py::get_authentication_port`` intercepts
``identity_mode == "iap"`` and returns ``IapAuthenticationAdapter``, a SECOND implementation
living in the API layer -- so on the request path the bound adapter's ``resolve`` was never
called, while its declaration was still what licensed the service to bind every interface.

Until 2026-08-26 the two did not agree about who the caller was:

===================  ==========================================  ==============================
                     ``adapters/gcp/iap_identity.py``            ``api/security.py``
===================  ==========================================  ==============================
subject              the ``email`` claim                         ``canonical_actor(iss, sub)``
tenant               the ``hd`` claim (commons passthrough)      a reviewed domain map, and an
                                                                 unmapped domain REFUSES
principals           ``user:<email>``                            ``user:<canonical>`` + groups
portal header        not read                                    read as a fallback
===================  ==========================================  ==============================

**The decision, and what this module now guards.** The API-layer half won, on the grounds
recorded in :mod:`cdd_sow_research.identity_policy`: it is the stricter half on every axis that
carries authority. It moved into that module and BOTH implementations now call it, so the
question is no longer which one runs but whether they can drift apart again. They cannot
silently: the tests below execute both and compare every ``Principal`` field.

The interception itself is deliberately still asserted. It is not a defect once both halves
decide identically -- the API layer needs richer evidence than ``IdentityPort.resolve`` returns
-- but it IS the mechanism that made a divergence invisible for as long as one existed, so it
stays visible in a test rather than being something a reader has to rediscover.

**Observed failing first:** against the pre-change tree,
``test_both_implementations_now_name_the_same_caller`` failed on every compared field.
"""

from __future__ import annotations

import base64
import json as _json
from typing import Any

import pytest
from hex_service_kit.federation import IAP_ASSERTION_HEADER, IAP_ISSUER

from cdd_sow_research.adapters.gcp.iap_identity import IapIdentityAdapter
from cdd_sow_research.api.security import (
    IapAuthenticationAdapter,
    canonical_actor,
    decode_canonical_actor,
)
from cdd_sow_research.domain.identity import IdentityError, RequestContext

_AUDIENCE = "/projects/1234567890/global/backendServices/42"
_EMAIL = "avery.stone@example-bank.test"
_SUB = "accounts.google.com:100000000000000000001"


def _token() -> str:
    header = (
        base64.urlsafe_b64encode(_json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        .decode()
        .rstrip("=")
    )
    return f"{header}.{base64.urlsafe_b64encode(b'{}').decode().rstrip('=')}.c2ln"


def _claims(**overrides: Any) -> dict[str, Any]:
    claims: dict[str, Any] = {
        "iss": IAP_ISSUER,
        "aud": _AUDIENCE,
        "sub": _SUB,
        "email": _EMAIL,
        "hd": "example-bank.test",
        "exp": 4102444800,
    }
    claims.update(overrides)
    return {name: value for name, value in claims.items() if value is not None}


def _bound_adapter(claims: dict[str, Any], mapping: dict[str, str] | None = None) -> Any:
    """The adapter ``config/settings.yaml`` binds to the identity port.

    It now reads the same two reviewed maps the API-layer half reads, so the fixture has to
    supply them. Passing ``_settings = None`` was possible only while this half decided the
    tenant from the assertion alone, which is the behaviour that was retired.
    """
    adapter = object.__new__(IapIdentityAdapter)
    adapter._settings = _Settings({} if mapping is None else mapping)
    adapter._audience = _AUDIENCE
    object.__setattr__(adapter, "_verify", lambda assertion: dict(claims))
    return adapter.resolve(RequestContext(headers={IAP_ASSERTION_HEADER: _token()}))


class _Identity:
    def __init__(self, mapping: dict[str, str]) -> None:
        self.iap_tenant_by_domain = mapping
        self.iap_groups_by_domain: dict[str, list[str]] = {}


class _Settings:
    def __init__(self, mapping: dict[str, str]) -> None:
        self.identity = _Identity(mapping)


def _api_adapter(claims: dict[str, Any], mapping: dict[str, str] | None = None) -> Any:
    """The implementation ``get_authentication_port`` actually returns for mode ``iap``."""
    adapter = object.__new__(IapAuthenticationAdapter)
    adapter._settings = _Settings({} if mapping is None else mapping)
    adapter._audience = _AUDIENCE
    object.__setattr__(adapter, "_verify", lambda assertion: dict(claims))
    return adapter.authenticate(RequestContext(headers={IAP_ASSERTION_HEADER: _token()}))


# --------------------------------------------------------------------------------------- #
# The wiring, asserted rather than described.
# --------------------------------------------------------------------------------------- #
def test_the_bound_identity_adapter_is_not_what_authenticates_an_iap_request() -> None:
    """The declaration and the decision live in different files, which is the finding."""
    import inspect

    from cdd_sow_research.api import security

    source = inspect.getsource(security.get_authentication_port)
    assert 'identity_mode == "iap"' in source
    assert "IapAuthenticationAdapter(container.settings)" in source
    # And the bound adapter is still what licenses the exposure posture.
    assert IapIdentityAdapter.end_user_auth == "verified"


# --------------------------------------------------------------------------------------- #
# The agreement, executed. Both halves are run and compared field by field.
# --------------------------------------------------------------------------------------- #
def _compared(principal: Any) -> tuple[Any, ...]:
    return (
        principal.subject,
        principal.principals,
        principal.tenant,
        principal.assurance,
        principal.source,
    )


def test_both_implementations_now_name_the_same_caller() -> None:
    claims = _claims()
    mapping = {"example-bank.test": "reference-bank"}

    bound = _bound_adapter(claims, mapping)
    served = _api_adapter(claims, mapping).principal

    assert _compared(bound) == _compared(served)
    assert bound.subject == canonical_actor(IAP_ISSUER, _SUB)
    assert decode_canonical_actor(bound.subject) == (IAP_ISSUER, _SUB)
    # The email claim is no longer anybody's subject, on either path.
    assert _EMAIL not in bound.subject and _EMAIL not in served.subject


def test_both_implementations_resolve_the_same_tenant_from_the_same_map() -> None:
    claims = _claims()
    mapping = {"example-bank.test": "reference-bank"}

    assert _bound_adapter(claims, mapping).tenant == "reference-bank"
    assert _api_adapter(claims, mapping).principal.tenant == "reference-bank"

    # And with no map configured, both fall back to the hosted domain identically.
    assert _bound_adapter(claims).tenant == "example-bank.test"
    assert _api_adapter(claims).principal.tenant == "example-bank.test"


def test_both_implementations_refuse_an_unmapped_tenant() -> None:
    """The refusal the commons expresses as an empty string, now held on BOTH paths.

    This is the one that mattered. The bound adapter used to hand back a principal partitioned
    by whatever ``hd`` said, so an assertion from a domain nobody reviewed still got a tenant.
    """
    claims = _claims(hd="somewhere-else.test")
    mapping = {"example-bank.test": "reference-bank"}

    with pytest.raises(IdentityError, match="did not resolve a policy-mapped tenant"):
        _bound_adapter(claims, mapping)
    with pytest.raises(IdentityError, match="did not resolve a policy-mapped tenant"):
        _api_adapter(claims, mapping)
