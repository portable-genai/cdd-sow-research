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


@respx.mock
def test_the_deployment_mints_an_id_token_for_the_service_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cloud Run accepts an ID token whose audience is the service URL, never a path under it."""
    monkeypatch.setenv(URL_ENV, "https://compliance-advisory-api.example.test/compliance/")
    audiences: list[str] = []

    def _fetch(audience: str) -> str:
        audiences.append(audience)
        return "id-token"

    monkeypatch.setattr(_s2s, "_fetch_id_token", _fetch)
    route = respx.post("https://compliance-advisory-api.example.test/compliance/ask").mock(
        return_value=httpx.Response(200, json=_ANSWER)
    )

    RemoteComplianceAdapter(_settings("gcp")).check("q", actor="a")

    assert audiences == ["https://compliance-advisory-api.example.test"]
    assert route.calls.last.request.headers["authorization"] == "Bearer id-token"


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
