"""Every profile that is not offline asks `compliance-advisory`, and says so on the wire.

Until this file the ``gcp`` profile bound ``compliance`` to the in-process stand-in in
``adapters/local/compliance.py``. Every deployed dossier's regulatory check was one canned
sentence with no citation, and because the service threw the answer away, nothing on the wire
could show it. ``live`` bound the same stand-in on the laptop, so the paired demonstration
compared two stand-ins and agreed about nothing.

These tests hold the binding, the three-state URL read and the outbound request shape.
"""

from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from cdd_sow_research.adapters.platform import _s2s
from cdd_sow_research.adapters.platform.remote_compliance import (
    AUDIENCE_ENV,
    RemoteComplianceAdapter,
    RemoteComplianceError,
)
from cdd_sow_research.api.app import create_app
from cdd_sow_research.config import Settings
from cdd_sow_research.domain.models import SourceType
from cdd_sow_research.envread import ConfiguredEmptyError

CONFIG_PATH = "config/settings.yaml"
REMOTE = "cdd_sow_research.adapters.platform.remote_compliance:RemoteComplianceAdapter"
URL_ENV = "RSK_COMPLIANCE_URL"

#: The profiles that must reach the real service. ``local`` is the offline gate and ``onprem``
#: the fail-fast placeholder; neither may make a network call.
NETWORKED_PROFILES = ("gcp", "live", "platform")

#: The deployed shape: compliance-advisory is an embedded app behind journey-portal's IAP edge, so
#: the base URL is that app's mount path on the edge and carries a path prefix.
EDGE_BASE = "https://rm.fictional-bank.example/apps/compliance-advisory/api"
#: The one bearer audience that edge accepts: the deployment's IAP OAuth client id.
EDGE_AUDIENCE = "1234567890-fictionaledgeclient.apps.googleusercontent.com"
#: What IAP compares its own INBOUND assertion against. It sits beside the client id in a
#: deployment record and is refused as a bearer audience.
BACKEND_SERVICE_AUDIENCE = "/projects/000000000000/global/backendServices/1111111111111111111"

_ANSWER = {
    "question": "What CDD expectations apply?",
    "answer": "Enhanced due diligence applies to a high-risk corporate customer.",
    "citations": [
        {
            "source_id": "notice-626",
            "regulator": "REGULATOR (FICTIONAL)",
            "jurisdiction": "SG",
            "title": "Notice on customer due diligence (FICTIONAL)",
            "url": "https://regulator.example/notice-626",
            "version": "2026-01",
            "page": 12,
            "snippet": "A financial institution shall perform enhanced CDD ...",
            "score": 0.81,
        }
    ],
    "web_citations": [],
    "confidence": 0.74,
    "requires_human_review": True,
    "caveats": [],
}


def _settings(profile: str) -> Settings:
    return replace(Settings.load(CONFIG_PATH), profile=profile)


def _bindings() -> dict[str, str]:
    return Settings.load(CONFIG_PATH).adapters["compliance"]


@pytest.fixture(autouse=True)
def _no_ambient_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("S2S_TOKEN", raising=False)
    monkeypatch.delenv("S2S_SIGNING_KEY", raising=False)
    # An ambient audience would make the "refuses without one" cases pass for the wrong reason.
    monkeypatch.delenv(AUDIENCE_ENV, raising=False)


# --------------------------------------------------------------------------------------- #
# The binding
# --------------------------------------------------------------------------------------- #
@pytest.mark.parametrize("profile", NETWORKED_PROFILES)
def test_networked_profiles_ask_compliance_advisory(profile: str) -> None:
    assert _bindings()[profile] == REMOTE


def test_the_deployment_is_never_answered_by_the_in_process_stand_in() -> None:
    """The regression this file exists for: ``gcp`` bound to ``adapters/local/compliance.py``."""
    binding = _bindings()["gcp"]
    assert not binding.startswith("cdd_sow_research.adapters.local."), binding
    assert "LocalComplianceClientAdapter" not in binding


def test_the_offline_gate_keeps_its_in_process_answer() -> None:
    assert _bindings()["local"] == (
        "cdd_sow_research.adapters.local.compliance:LocalComplianceClientAdapter"
    )


# --------------------------------------------------------------------------------------- #
# Three states for the service URL, and no default to fall back on
# --------------------------------------------------------------------------------------- #
@pytest.mark.parametrize("profile", NETWORKED_PROFILES)
def test_an_unnamed_compliance_service_refuses_to_construct(
    profile: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A networked profile names the service it asks; it never inherits a localhost guess."""
    monkeypatch.delenv(URL_ENV, raising=False)
    with pytest.raises(ConfiguredEmptyError, match=URL_ENV):
        RemoteComplianceAdapter(_settings(profile))


@pytest.mark.parametrize("profile", NETWORKED_PROFILES)
def test_an_emptied_compliance_url_refuses_to_construct(
    profile: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(URL_ENV, "  ")
    with pytest.raises(ConfiguredEmptyError, match=URL_ENV):
        RemoteComplianceAdapter(_settings(profile))


def test_a_plaintext_url_to_a_real_host_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(URL_ENV, "http://compliance-advisory-api.example.test")
    with pytest.raises(ValueError, match="https"):
        RemoteComplianceAdapter(_settings("gcp"))


# --------------------------------------------------------------------------------------- #
# What goes on the wire, and what comes back
# --------------------------------------------------------------------------------------- #
@respx.mock
def test_the_request_asserts_no_actor_and_the_citations_survive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """compliance-advisory resolves its principal server-side and ignores a body actor, so
    sending one only suggests the receiver trusts it."""
    monkeypatch.setenv(URL_ENV, "http://127.0.0.1:8091")
    route = respx.post("http://127.0.0.1:8091/ask").mock(
        return_value=httpx.Response(200, json=_ANSWER)
    )

    answer = RemoteComplianceAdapter(_settings("live")).check(
        "What CDD expectations apply?", actor="analyst@bank.test"
    )

    body = json.loads(route.calls.last.request.content)
    assert "actor" not in body
    assert body["question"] == "What CDD expectations apply?"
    assert len(answer.citations) == 1
    citation = answer.citations[0]
    assert citation.source_type is SourceType.REGULATION
    assert citation.title == "Notice on customer due diligence (FICTIONAL)"
    assert citation.page == 12
    assert answer.requires_human_review is True


@respx.mock
def test_an_ungrounded_refusal_is_an_error_not_an_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(URL_ENV, "http://127.0.0.1:8091")
    respx.post("http://127.0.0.1:8091/ask").mock(
        return_value=httpx.Response(
            422, json={"error": "ungrounded", "requires_human_review": True, "citations": []}
        )
    )
    with pytest.raises(RemoteComplianceError, match="422"):
        RemoteComplianceAdapter(_settings("live")).check("q", actor="a")


# --------------------------------------------------------------------------------------- #
# The deployed leg goes through the portal's IAP edge
# --------------------------------------------------------------------------------------- #
def _record_minted(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Capture the audiences a run mints for, without a cloud SDK or a credential anywhere."""
    audiences: list[str] = []

    def _fetch(audience: str) -> str:
        audiences.append(audience)
        return "id-token"

    monkeypatch.setattr(_s2s, "_fetch_id_token", _fetch)
    return audiences


@respx.mock
def test_the_deployment_mints_a_token_for_the_configured_iap_client_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The only bearer audience the portal's IAP edge accepts is its OAuth client id.

    The first attempt at this leg minted for the sibling service's own origin and assumed a
    direct service-to-service call, which cannot work in four independent ways: the embedded
    APIs take internal traffic only, only the portal's service account may invoke them, this
    service has no VPC egress, and compliance-advisory's managed identity accepts an IAP
    assertion and nothing else. The call goes through the edge, and the edge wants the client id.
    """
    monkeypatch.setenv(URL_ENV, EDGE_BASE)
    monkeypatch.setenv(AUDIENCE_ENV, EDGE_AUDIENCE)
    audiences = _record_minted(monkeypatch)
    route = respx.post(f"{EDGE_BASE}/ask").mock(return_value=httpx.Response(200, json=_ANSWER))

    RemoteComplianceAdapter(_settings("gcp")).check("q", actor="a")

    assert audiences == [EDGE_AUDIENCE], "a token minted for anything else is refused at the edge"
    assert route.calls.last.request.headers["authorization"] == "Bearer id-token"


@respx.mock
def test_the_request_path_is_ask_inside_the_app_mount(monkeypatch: pytest.MonkeyPatch) -> None:
    """A base URL with a path prefix keeps it, so ``/ask`` lands inside the app's mount.

    Appending to the ORIGIN instead would POST to the portal's own root, where a compliance
    question is answered by a portal route rather than by compliance-advisory.
    """
    monkeypatch.setenv(URL_ENV, EDGE_BASE)
    monkeypatch.setenv(AUDIENCE_ENV, EDGE_AUDIENCE)
    _record_minted(monkeypatch)
    route = respx.post(f"{EDGE_BASE}/ask").mock(return_value=httpx.Response(200, json=_ANSWER))

    RemoteComplianceAdapter(_settings("gcp")).check("q", actor="a")

    assert str(route.calls.last.request.url) == (
        "https://rm.fictional-bank.example/apps/compliance-advisory/api/ask"
    )


def test_a_base_url_with_a_path_prefix_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """The mount path is the whole point of the edge leg, and a trailing slash is normalised."""
    monkeypatch.setenv(AUDIENCE_ENV, EDGE_AUDIENCE)
    monkeypatch.setenv(URL_ENV, f"{EDGE_BASE}/")

    adapter = RemoteComplianceAdapter(_settings("gcp"))

    assert adapter._base_url == EDGE_BASE


def test_a_deployment_without_the_iap_audience_refuses_to_construct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Naming the service is not enough: an unnamed audience is refused at the edge on every
    dossier, and this process never learns why, so it refuses here and names the variable."""
    monkeypatch.setenv(URL_ENV, EDGE_BASE)
    monkeypatch.delenv(AUDIENCE_ENV, raising=False)

    with pytest.raises(ConfiguredEmptyError, match=AUDIENCE_ENV):
        RemoteComplianceAdapter(_settings("gcp"))


def test_an_emptied_iap_audience_refuses_to_construct(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(URL_ENV, EDGE_BASE)
    monkeypatch.setenv(AUDIENCE_ENV, "   ")

    with pytest.raises(ConfiguredEmptyError, match=AUDIENCE_ENV):
        RemoteComplianceAdapter(_settings("gcp"))


@pytest.mark.parametrize("profile", ("live", "platform"))
def test_an_emptied_audience_refuses_even_where_absence_is_legitimate(
    profile: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three states, not two, on the profiles where an ABSENT audience is the correct posture.

    Unset means "this receiver is not behind IAP" and falls back to its origin, or to no token
    at all on loopback. Emptied means somebody configured an audience and it names nothing: it
    must not inherit the unset posture, because that is how a deployment intending the edge
    quietly sends a token the edge refuses, or none.
    """
    monkeypatch.setenv(URL_ENV, "https://compliance-advisory-api.example.test")
    monkeypatch.setenv(AUDIENCE_ENV, "   ")

    with pytest.raises(ConfiguredEmptyError, match=AUDIENCE_ENV):
        RemoteComplianceAdapter(_settings(profile))


def test_the_backend_service_path_is_refused_as_a_bearer_audience(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deployment record carries both audiences, and only one is a bearer audience."""
    monkeypatch.setenv(URL_ENV, EDGE_BASE)
    monkeypatch.setenv(AUDIENCE_ENV, BACKEND_SERVICE_AUDIENCE)

    with pytest.raises(ValueError, match="backend-service path"):
        RemoteComplianceAdapter(_settings("gcp"))


def test_the_platform_profile_still_calls_a_sibling_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``platform`` means a thin delegate to a sibling contract, reached directly, and this leg
    does not redefine it: with no audience configured it mints for the receiver's own origin,
    exactly like the five platform adapters beside it."""
    monkeypatch.setenv(URL_ENV, "https://compliance-advisory-api.example.test")
    audiences = _record_minted(monkeypatch)
    with respx.mock:
        respx.post("https://compliance-advisory-api.example.test/ask").mock(
            return_value=httpx.Response(200, json=_ANSWER)
        )
        RemoteComplianceAdapter(_settings("platform")).check("q", actor="a")

    assert audiences == ["https://compliance-advisory-api.example.test"]


@respx.mock
def test_the_laptop_call_carries_no_token_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """Under ``live`` the launcher runs compliance-advisory on loopback with no IAP in front of
    it, so there is nothing to authenticate to and no audience to name."""
    monkeypatch.setenv(URL_ENV, "http://127.0.0.1:8080")
    minted = _record_minted(monkeypatch)
    route = respx.post("http://127.0.0.1:8080/ask").mock(
        return_value=httpx.Response(200, json=_ANSWER)
    )

    RemoteComplianceAdapter(_settings("live")).check("q", actor="a")

    assert minted == []
    assert "authorization" not in route.calls.last.request.headers


# --------------------------------------------------------------------------------------- #
# Refused at boot, not on the first dossier
# --------------------------------------------------------------------------------------- #
def test_a_networked_process_without_the_service_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bound at startup, so a deployment that never named the service fails its revision and
    says which variable, instead of answering every assessment with a 500."""
    monkeypatch.delenv(URL_ENV, raising=False)
    # A startable live process in every other respect, so the refusal is this one.
    monkeypatch.setenv("CDD_IDENTITY_PROFILE", "local-persona")
    monkeypatch.setenv("CDD_CHANNEL_PROFILE", "standalone")

    with (
        pytest.raises(RuntimeError, match=URL_ENV),
        TestClient(create_app(_settings("live")), client=("127.0.0.1", 50000)),
    ):
        pass


def test_a_deployment_without_the_audience_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of the leg fails the revision at boot too, and names its own variable.

    A revision that starts without it looks healthy: it serves, it asks, and the edge refuses
    every question, so the dossier reports NOT CHECKED with no operator-visible cause.
    """
    monkeypatch.setenv(URL_ENV, EDGE_BASE)
    monkeypatch.delenv(AUDIENCE_ENV, raising=False)
    monkeypatch.setenv("CDD_IDENTITY_PROFILE", "local-persona")
    monkeypatch.setenv("CDD_CHANNEL_PROFILE", "standalone")

    # A startable managed process in every other respect, so the refusal under test is this one:
    # the gcp profile also refuses the documented placeholder project id.
    settings = replace(_settings("gcp"), project_id="fictional-doc1-production")

    with (
        pytest.raises(RuntimeError, match=AUDIENCE_ENV),
        TestClient(create_app(settings), client=("127.0.0.1", 50000)),
    ):
        pass
