"""This repository has TWO IAP claim halves, and they disagree. Recorded, not normalised.

Adopting ``hex_service_kit.federation`` here meant executing it against the shipped adapter,
and the first thing execution turned up was not a kit gap. It was that
``adapters/gcp/iap_identity.py`` is not the code that authenticates an IAP request.

``config/settings.yaml`` binds ``identity.bindings.iap`` to
:class:`~cdd_sow_research.adapters.gcp.iap_identity.IapIdentityAdapter`, so that adapter is
what ``container.identity`` holds, and its ``end_user_auth = VERIFIED`` class attribute is what
stands the exposure guard down. But ``api/security.py::get_authentication_port`` intercepts
``identity_mode == "iap"`` and returns ``IapAuthenticationAdapter``, a SECOND implementation
living in the API layer. So on the request path the bound adapter's ``resolve`` is never
called, while its declaration is still what licenses the service to bind every interface.

The two do not agree about who the caller is:

===================  ==========================================  ==============================
                     ``adapters/gcp/iap_identity.py``            ``api/security.py``
===================  ==========================================  ==============================
subject              the ``email`` claim                         ``canonical_actor(iss, sub)``
tenant               the ``hd`` claim (commons passthrough)      a reviewed domain map, and an
                                                                 unmapped domain REFUSES
principals           ``user:<email>``                            ``user:<canonical>`` + groups
portal header        not read                                    read as a fallback
===================  ==========================================  ==============================

Only one of them can be adopted onto the commons as it stands. The claim half in the adapter
now is; the one in the API layer cannot be, because ``principal_from_iap_claims`` reads
``email or sub`` as the subject and offers no knob for a canonical ``(iss, sub)`` actor, and
because refusing an unmapped tenant outright is a policy the commons expresses as an empty
string rather than as a refusal.

Neither half is changed here. What is added is this module, so that a divergence two files
apart is a thing the suite states rather than a thing somebody has to find twice.
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


def _bound_adapter(claims: dict[str, Any]) -> Any:
    """The adapter ``config/settings.yaml`` binds to the identity port."""
    adapter = object.__new__(IapIdentityAdapter)
    adapter._settings = None
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
# The disagreement, executed.
# --------------------------------------------------------------------------------------- #
def test_the_two_implementations_name_the_same_caller_differently() -> None:
    claims = _claims()
    bound = _bound_adapter(claims)
    served = _api_adapter(claims).principal

    assert bound.subject == _EMAIL
    assert decode_canonical_actor(bound.subject) is None

    assert served.subject == canonical_actor(IAP_ISSUER, _SUB)
    assert decode_canonical_actor(served.subject) == (IAP_ISSUER, _SUB)

    assert bound.principals == (f"user:{_EMAIL}",)
    assert served.principals == (f"user:{served.subject}",)


def test_the_two_implementations_resolve_the_tenant_differently() -> None:
    """With no map the two agree by accident; with one configured they part."""
    claims = _claims()
    assert _bound_adapter(claims).tenant == "example-bank.test"
    assert _api_adapter(claims).principal.tenant == "example-bank.test"

    mapping = {"example-bank.test": "reference-bank"}
    assert _bound_adapter(claims).tenant == "example-bank.test"
    assert _api_adapter(claims, mapping).principal.tenant == "reference-bank"


def test_only_the_api_implementation_refuses_an_unmapped_tenant() -> None:
    """A refusal the commons expresses as an empty string, which is why it cannot adopt it."""
    claims = _claims(hd="somewhere-else.test")
    mapping = {"example-bank.test": "reference-bank"}
    assert _bound_adapter(claims).tenant == "somewhere-else.test"
    with pytest.raises(IdentityError, match="did not resolve a policy-mapped tenant"):
        _api_adapter(claims, mapping)
