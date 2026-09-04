"""The embedding host's assertion header: accepted, and verified exactly like the standard one.

``x-goog-*`` is Google's reserved namespace, and the serverless frontend removes those headers
from a request entering a service so that only the platform can set them. The consequence for a
same-origin embedding host is that it cannot forward the assertion IAP handed it: the portal
injected ``x-goog-iap-jwt-assertion``, the frontend dropped it, and this service refused with
"missing IAP assertion header; request did not pass through IAP" -- about a request that had
passed through IAP one hop earlier.

So the host also sends the assertion under ``x-portal-iap-assertion``, and this reads it as a
FALLBACK. The tests below pin the two properties that make that safe: the standard header still
wins, and the fallback buys no relaxation -- the assertion is verified the same way either way.
"""

from __future__ import annotations

import pytest
from hex_service_kit.identity import IdentityError, RequestContext

from cdd_sow_research.api.security import (
    _IAP_ASSERTION_HEADER,
    _PORTAL_ASSERTION_HEADER,
)
from cdd_sow_research.config import (
    IAP_GROUPS_ENV,
    IAP_TENANT_DOMAINS_ENV,
    resolve_iap_groups_by_domain,
    resolve_iap_tenant_by_domain,
)


def _authenticator():
    from cdd_sow_research.api import security

    for name in dir(security):
        obj = getattr(security, name)
        if isinstance(obj, type) and "assertion = ctx.header" in _source_of(obj):
            return obj
    raise AssertionError("no authenticator class reads the assertion header")


def _source_of(obj: type) -> str:
    import inspect

    try:
        return inspect.getsource(obj)
    except (OSError, TypeError):
        return ""


def test_the_two_header_names_are_distinct_and_the_portal_one_is_not_reserved() -> None:
    assert _IAP_ASSERTION_HEADER == "x-goog-iap-jwt-assertion"
    assert _PORTAL_ASSERTION_HEADER == "x-portal-iap-assertion"
    assert not _PORTAL_ASSERTION_HEADER.startswith("x-goog-"), (
        "the whole point of the fallback name is that it is outside the namespace the serverless "
        "frontend strips; putting it back inside would reintroduce the bug it fixes"
    )


@pytest.mark.parametrize("header", [_IAP_ASSERTION_HEADER, _PORTAL_ASSERTION_HEADER])
def test_either_header_reaches_the_verifier_and_neither_bypasses_it(header: str) -> None:
    """An assertion under EITHER name is HANDED TO THE VERIFIER, and never accepted unverified.

    The gate is SDK-free, so ``google-auth`` is absent and the lazy import inside the verifier
    raises. That import is the assertion under test: reaching it proves the header was read and
    passed on, and the alternative outcomes are the two that would be defects -- a
    ``missing IAP assertion header`` refusal (the header was ignored) or a returned identity (the
    assertion was trusted without verification).
    """

    authenticator = _authenticator()
    instance = authenticator.__new__(authenticator)
    instance._settings = object()
    instance._audience = "test-audience"

    with pytest.raises((IdentityError, ModuleNotFoundError)) as raised:
        result = instance.authenticate(RequestContext(headers={header: "not-a-jwt"}))
        raise AssertionError(f"an unverified assertion produced an identity: {result!r}")

    assert "missing IAP assertion header" not in str(raised.value), (
        f"{header} was not read: the request was refused as though it carried no assertion"
    )


def test_neither_header_present_is_still_a_refusal() -> None:
    authenticator = _authenticator()
    instance = authenticator.__new__(authenticator)
    instance._settings = object()
    instance._audience = "test-audience"
    with pytest.raises(IdentityError, match="missing IAP assertion header"):
        instance.authenticate(RequestContext(headers={}))


# --------------------------------------------------------------------------------------- #
# The tenant behind the assertion.
# --------------------------------------------------------------------------------------- #
def test_the_iap_tenant_map_is_read_in_three_states(monkeypatch: pytest.MonkeyPatch) -> None:
    """UNSET keeps the reviewed map; SET-AND-EMPTY is refused, never treated as "no mapping".

    Empty is the permissive branch here: with no map the tenant falls back to whatever Google
    calls the sign-in domain, so an operator who emptied the variable would silently stamp
    evidence with a Workspace domain instead of the institution's reviewed tenant id.
    """

    monkeypatch.delenv(IAP_TENANT_DOMAINS_ENV, raising=False)
    assert resolve_iap_tenant_by_domain({"bank.example": "demo-bank"}) == {
        "bank.example": "demo-bank"
    }

    monkeypatch.setenv(IAP_TENANT_DOMAINS_ENV, '{"other.example": "other-bank"}')
    assert resolve_iap_tenant_by_domain({"bank.example": "demo-bank"}) == {
        "other.example": "other-bank"
    }

    monkeypatch.setenv(IAP_TENANT_DOMAINS_ENV, "")
    with pytest.raises(ValueError, match="empty value"):
        resolve_iap_tenant_by_domain({})


@pytest.mark.parametrize(
    "raw",
    ['{"": "demo-bank"}', '{"bank.example": ""}', '["bank.example"]', "not-json"],
    ids=["blank-domain", "blank-tenant", "not-an-object", "not-json"],
)
def test_a_half_configured_iap_tenant_map_is_refused(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv(IAP_TENANT_DOMAINS_ENV, raw)
    with pytest.raises(ValueError):
        resolve_iap_tenant_by_domain({})


def test_the_domain_is_case_folded_so_a_capitalised_hd_still_maps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(IAP_TENANT_DOMAINS_ENV, '{"Bank.Example": "demo-bank"}')
    assert resolve_iap_tenant_by_domain({}) == {"bank.example": "demo-bank"}


# --------------------------------------------------------------------------------------- #
# Readiness, on a path the platform does not reserve.
# --------------------------------------------------------------------------------------- #
def test_readiness_answers_on_both_the_probe_path_and_the_versioned_one() -> None:
    """``/healthz`` for the container's own probe, ``/v1/healthz`` for everyone reaching it.

    On Google's serverless platform a request to ``<service>/healthz`` is answered by the
    frontend and never reaches the container, so an embedding host proxying to this service
    cannot ask it whether it is ready: the console showed "Connecting to cdd-sow-research..."
    indefinitely
    against a service that was healthy and serving every other route. ``/healthzz`` and
    ``/v1/healthz`` arrive normally, which is how the reserved path was identified.

    Both paths must return the SAME payload; an alias that drifts is worse than no alias,
    because the two answers would disagree about what "ready" means.
    """

    from fastapi.testclient import TestClient

    from cdd_sow_research.api.app import app

    client = TestClient(app, client=("127.0.0.1", 50000))
    probe = client.get("/healthz", headers={"X-Dev-Persona": "analyst"})
    versioned = client.get("/v1/healthz", headers={"X-Dev-Persona": "analyst"})

    assert probe.status_code == 200, probe.text
    assert versioned.status_code == 200, versioned.text
    assert probe.json() == versioned.json()


# --------------------------------------------------------------------------------------- #
# The groups behind the assertion.
# --------------------------------------------------------------------------------------- #
def test_the_iap_groups_map_is_read_in_three_states(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(IAP_GROUPS_ENV, raising=False)
    assert resolve_iap_groups_by_domain({"bank.example": ["group:cdd-analyst"]}) == {
        "bank.example": ("group:cdd-analyst",)
    }

    monkeypatch.setenv(IAP_GROUPS_ENV, '{"svc.example": ["group:cdd-analyst", "group:audit"]}')
    assert resolve_iap_groups_by_domain({"bank.example": ["group:cdd-analyst"]}) == {
        "svc.example": ("group:cdd-analyst", "group:audit")
    }

    monkeypatch.setenv(IAP_GROUPS_ENV, "")
    with pytest.raises(ValueError, match="empty value"):
        resolve_iap_groups_by_domain({})


@pytest.mark.parametrize(
    "raw",
    [
        '{"bank.example": []}',
        '{"bank.example": ""}',
        '{"": ["group:audit"]}',
        '{"a.example": [""]}',
    ],
    ids=["empty-list", "string-not-list", "blank-domain", "blank-group"],
)
def test_a_half_configured_iap_groups_map_is_refused(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """An empty group list is the shape that reads as "configured" and grants nothing."""

    monkeypatch.setenv(IAP_GROUPS_ENV, raw)
    with pytest.raises(ValueError):
        resolve_iap_groups_by_domain({})


def test_an_unmapped_domain_holds_no_groups(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail closed: the map grants, it never falls back to granting everyone something."""

    monkeypatch.setenv(IAP_GROUPS_ENV, '{"bank.example": ["group:cdd-analyst"]}')
    assert resolve_iap_groups_by_domain({}).get("other.example") is None


# --------------------------------------------------------------------------------------- #
# The placeholder project id.
# --------------------------------------------------------------------------------------- #
def test_the_placeholder_project_is_refused_in_a_managed_profile() -> None:
    """A documented placeholder must not reach a live API call.

    ``config/settings.yaml`` ships ``project_id: ${GOOGLE_CLOUD_PROJECT:-your-gcp-project}``,
    which is right on a laptop where nothing calls a cloud API. Unset in a managed deployment it
    travelled into a real request, and the agent answered 500 with "projects/your-gcp-project
    does not exist" on the first dossier build -- a message that reads as a broken service rather
    than an unset environment variable.
    """

    from dataclasses import replace

    from cdd_sow_research.config import MANAGED_PROFILES, PLACEHOLDER_PROJECT_ID, Settings

    # A managed profile also needs a channel and an identity mode named, and those checks run
    # FIRST because a mis-wired channel is the more fundamental error. They are resolved when
    # the settings are LOADED, so they must be named before the load, not before the validate.
    for profile in sorted(MANAGED_PROFILES):
        with pytest.MonkeyPatch.context() as env:
            env.setenv("CDD_CHANNEL_PROFILE", "native")
            env.setenv("CDD_IDENTITY_PROFILE", "iap")
            managed = replace(Settings.load(), profile=profile, project_id=PLACEHOLDER_PROJECT_ID)
            with pytest.raises(ValueError, match="placeholder"):
                managed.validate_deployment()


def test_the_placeholder_project_is_fine_on_a_laptop() -> None:
    """The same value in the local profile is the documented default, not a defect."""

    from dataclasses import replace

    from cdd_sow_research.config import PLACEHOLDER_PROJECT_ID, Settings

    with pytest.MonkeyPatch.context() as env:
        env.setenv("CDD_CHANNEL_PROFILE", "native")
        env.setenv("CDD_IDENTITY_PROFILE", "local-persona")
        local = replace(Settings.load(), profile="local", project_id=PLACEHOLDER_PROJECT_ID)
        local.validate_deployment()
