"""A missing PyJWT is an environment fault (503), never a malformed-token fault (401).

Deliberately NOT guarded by ``pytest.importorskip("jwt")``: this module runs on the core,
SDK-free gate (where the ``oidc`` extra is absent) AND on the ``oidc`` CI leg (where it is
present). It forces the ``import jwt`` to fail regardless of whether PyJWT is installed, so
the classification is asserted deterministically in either environment.

The bug this pins: ``_protected_header`` once wrapped ``import jwt`` inside a broad
``except Exception`` that raised ``IdentityError('... protected header is invalid')``. In a
clean clone without the ``oidc`` extra, a well-formed assertion was then rejected as
malformed (a 401 blaming the caller) when the real fault was an operator one (PyJWT not
installed, which must be a 503 naming the missing extra).
"""

from __future__ import annotations

import builtins

import pytest

from cdd_sow_research.adapters.oidc.private_key_jwt import (
    EndUserAuthUnavailableError,
    InMemoryClientAssertionReplayStore,
    PinnedClientKey,
    PrivateKeyJwtClientPolicy,
    PrivateKeyJwtVerifier,
    _protected_header,
)
from cdd_sow_research.domain.identity import IdentityError

# A structurally valid compact JWS (three base64url segments, header decodes to a JSON
# object): shape and header are fine, so nothing but a missing library can reject it.
_WELL_FORMED = "eyJhbGciOiJSUzI1NiIsImtpZCI6ImJmZi1rZXktMSJ9.eyJqdGkiOiJBIn0.c2lnbmF0dXJl"


@pytest.fixture
def _no_pyjwt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force ``import jwt`` to raise ImportError, as in a clone without the oidc extra."""
    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object):
        if name == "jwt" or name.startswith("jwt."):
            raise ImportError("No module named 'jwt'")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", fake_import)


def test_the_two_faults_are_distinct_types() -> None:
    """The environment fault must not be catchable as the caller fault, or an ``except
    IdentityError`` at the API boundary would silently turn a 503 into a 401."""
    assert not issubclass(EndUserAuthUnavailableError, IdentityError)
    assert not issubclass(IdentityError, EndUserAuthUnavailableError)


def test_missing_pyjwt_on_a_well_formed_token_is_unavailable_not_malformed(
    _no_pyjwt: None,
) -> None:
    with pytest.raises(EndUserAuthUnavailableError) as captured:
        _protected_header(_WELL_FORMED)
    message = str(captured.value)
    assert "oidc" in message  # names the missing extra so an operator can fix the deploy
    assert "PyJWT" in message


def test_missing_pyjwt_surfaces_through_verify(_no_pyjwt: None) -> None:
    """End to end: the verifier raises the environment fault, not an IdentityError."""
    policy = PrivateKeyJwtClientPolicy(
        client_id="demo-bank-portal-bff",
        audience="https://doc1.example/agent/api/v1/embed/grants",
        keys=(PinnedClientKey(kid="bff-key-1", algorithm="RS256", public_jwk={"kty": "RSA"}),),
    )
    verifier = PrivateKeyJwtVerifier((policy,), InMemoryClientAssertionReplayStore())

    from datetime import UTC, datetime

    with pytest.raises(EndUserAuthUnavailableError):
        verifier.verify(
            _WELL_FORMED,
            expected_client_id="demo-bank-portal-bff",
            as_of=datetime(2026, 7, 27, 12, 0, tzinfo=UTC),
        )


def test_shape_error_stays_a_caller_fault_even_without_pyjwt(_no_pyjwt: None) -> None:
    """A token that is not even a compact JWS is rejected before any import, so it remains a
    401-class ``IdentityError`` regardless of whether the library is installed."""
    with pytest.raises(IdentityError, match="compact signed JWT"):
        _protected_header("not-a-jwt")


def test_http_error_maps_the_two_faults_to_503_and_401() -> None:
    """The embed broker maps the environment fault to 503 and the caller fault to 401."""
    from cdd_sow_research.api.embed import _http_error

    unavailable = _http_error(EndUserAuthUnavailableError("PyJWT missing"))
    assert unavailable.status_code == 503

    malformed = _http_error(IdentityError("private_key_jwt protected header is invalid"))
    assert malformed.status_code == 401
