"""The CLAIM half of the IAP adapter: ONE reviewed decision, in one place.

Signature verification stays in the adapter, because it needs a cloud SDK and
``hex_service_kit``'s core is pure standard library. Everything AFTER the signature -- which
string is the subject, which partition is the tenant, which entitlement principals the caller
holds -- now lives in :mod:`cdd_sow_research.identity_policy` and is shared with
``api/security.py``.

**What changed on 2026-08-26, and why this file no longer asserts what it used to.** This
module previously asserted that the adapter's claim half WAS
``hex_service_kit.federation.principal_from_iap_claims``. That was true and it was not the
code authenticating anything: ``api/security.py::get_authentication_port`` intercepted
``identity_mode == "iap"`` and returned a second implementation, so the commons decision this
file guarded was guarded on a path no request took. The divergence was recorded rather than
normalised, because choosing between the two was a decision; the decision taken was the
API-layer half, on the grounds in ``identity_policy``'s docstring, and the commons half is what
gave way.

The commons is not wrong and this is not a repudiation of adopting it. What the commons cannot
express is named in the portfolio backlog: a canonical ``(iss, sub)`` subject, and an unmapped
tenant that REFUSES rather than resolving to an empty string. Both are load-bearing here.

**Observed failing first.** Against the pre-change adapter,
``test_the_subject_is_the_immutable_pair_and_not_the_email_claim`` returned the ``email``
claim, and ``test_an_unmapped_domain_is_refused_rather_than_partitioned_by_hd`` returned a
principal partitioned by the raw ``hd`` claim instead of raising.
"""

from __future__ import annotations

import base64
import json as _json
from types import SimpleNamespace
from typing import Any

import pytest
from hex_service_kit.federation import IAP_ASSERTION_HEADER, IAP_ISSUER

from cdd_sow_research.adapters.gcp.iap_identity import IapIdentityAdapter
from cdd_sow_research.domain.identity import IdentityError, RequestContext
from cdd_sow_research.identity_policy import canonical_actor, decode_canonical_actor

_AUDIENCE = "/projects/1234567890/global/backendServices/42"
_EMAIL = "avery.stone@example-bank.test"
_SUB = "accounts.google.com:100000000000000000001"
_DOMAIN = "example-bank.test"
_TENANT = "reference-bank"
_GROUPS = ("group:cdd-analyst", "group:cdd-approver")


def _token() -> str:
    """A structurally real compact JWS. Only the header is read, and nothing is signed."""
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
        "hd": _DOMAIN,
        "exp": 4102444800,
    }
    claims.update(overrides)
    return {name: value for name, value in claims.items() if value is not None}


def settings(
    *,
    tenants: dict[str, str] | None = None,
    groups: dict[str, tuple[str, ...]] | None = None,
) -> Any:
    """Only the two reviewed maps the claim half reads, and nothing else in the container."""
    return SimpleNamespace(
        identity=SimpleNamespace(
            iap_tenant_by_domain=dict(tenants if tenants is not None else {_DOMAIN: _TENANT}),
            iap_groups_by_domain=dict(groups if groups is not None else {_DOMAIN: _GROUPS}),
        )
    )


def _adapter(configured: Any = None, audience: str = _AUDIENCE) -> IapIdentityAdapter:
    adapter = object.__new__(IapIdentityAdapter)
    adapter._settings = configured if configured is not None else settings()
    adapter._audience = audience
    adapter._audience_configured_empty = False
    return adapter


def _resolve(claims: dict[str, Any], adapter: IapIdentityAdapter | None = None) -> Any:
    """Run the shipped adapter's claim half over ``claims``, with the cryptography stubbed.

    Stubbing ``_verify`` is what makes the claim half reachable without a network, a credential
    or a cloud SDK; it is not a way of skipping a check, because every refusal the verifier owns
    is exercised by the crypto suite instead.
    """
    adapter = adapter or _adapter()
    object.__setattr__(adapter, "_verify", lambda assertion: dict(claims))
    return adapter.resolve(RequestContext(headers={IAP_ASSERTION_HEADER: _token()}))


def test_the_subject_is_the_immutable_pair_and_not_the_email_claim() -> None:
    principal = _resolve(_claims())

    assert principal.subject == canonical_actor(IAP_ISSUER, _SUB)
    assert decode_canonical_actor(principal.subject) == (IAP_ISSUER, _SUB)
    # The email is a display string a directory can reassign to a different person. It must not
    # be what an audit record attributes an action to.
    assert _EMAIL not in principal.subject


def test_the_verified_subject_holds_its_own_principal_and_its_reviewed_groups() -> None:
    principal = _resolve(_claims())

    assert principal.principals == (f"user:{principal.subject}", *_GROUPS)
    # user:<subject> alone satisfies no case-access role, which is what left every signed-in
    # analyst refused every case they named before the groups were stated.
    assert len(principal.principals) > 1


def test_the_tenant_comes_from_the_reviewed_map_and_not_from_the_hosted_domain() -> None:
    principal = _resolve(_claims())

    assert principal.tenant == _TENANT
    assert principal.tenant != _DOMAIN


def test_an_unmapped_domain_is_refused_rather_than_partitioned_by_hd() -> None:
    configured = settings(tenants={"another-bank.test": "another"})

    with pytest.raises(IdentityError, match="policy-mapped tenant"):
        _resolve(_claims(), _adapter(configured))


def test_with_no_map_configured_the_hosted_domain_is_still_the_tenant() -> None:
    """An existing deployment that configured no map is unchanged by the map existing."""
    principal = _resolve(_claims(), _adapter(settings(tenants={})))

    assert principal.tenant == _DOMAIN


def test_a_machine_identity_is_admitted_only_by_naming_its_email_domain() -> None:
    machine = _claims(hd=None, email="runner@demo.iam.gserviceaccount.com")
    configured = settings(
        tenants={"demo.iam.gserviceaccount.com": _TENANT},
        groups={"demo.iam.gserviceaccount.com": ("group:cdd-analyst",)},
    )

    principal = _resolve(machine, _adapter(configured))

    assert principal.tenant == _TENANT
    assert principal.principals[1:] == ("group:cdd-analyst",)
    # And with nothing naming it, the same caller is refused rather than given a partition.
    with pytest.raises(IdentityError, match="policy-mapped tenant"):
        _resolve(machine, _adapter(settings(tenants={})))


def test_a_claim_set_from_another_issuer_cannot_become_a_principal() -> None:
    with pytest.raises(IdentityError):
        _resolve(_claims(iss="https://accounts.google.com"))


def test_a_verified_assertion_that_names_nobody_is_refused() -> None:
    with pytest.raises(IdentityError):
        _resolve(_claims(sub=None))
