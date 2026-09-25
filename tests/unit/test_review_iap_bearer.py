"""The console hand-off through the portal's IAP edge: a minted bearer, and a boot check.

Under ``gcp`` the deployed human-review-console is an embedded app behind `journey-portal`'s IAP
edge, which accepts only a Google-signed ID token minted for the IAP OAuth client id. So the
managed review router mints one per submission for ``HUMAN_REVIEW_IAP_AUDIENCE`` (with the same
helper the compliance leg uses), keeps the static ``CDD_S2S_TOKEN`` bearer when the audience is
unset, and the gcp boot check requires the audience beside ``HUMAN_REVIEW_URL`` while routing is
on. The minting function is faked: nothing here reaches Google.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
import review_kit.client
from review_kit import Review

from cdd_sow_research.adapters.gcp.review_router import PlatformReviewRouter
from cdd_sow_research.adapters.platform import _s2s
from cdd_sow_research.config import (
    HUMAN_REVIEW_IAP_AUDIENCE_ENV,
    HUMAN_REVIEW_URL_ENV,
    REVIEW_ROUTING_ENV,
    Settings,
)
from cdd_sow_research.domain.models import (
    CDDCase,
    RiskBand,
    RiskRating,
    SourceOfWealthNarrative,
    Subject,
    SubjectType,
)
from cdd_sow_research.envread import ConfiguredEmptyError

CONFIG = "config/settings.yaml"
EDGE_URL = "https://rm.example.test/apps/human-review-console/api"
AUDIENCE = "1234567890-fictionaledgeclient.apps.googleusercontent.com"
BACKEND_PATH = "/projects/000000000000/global/backendServices/1111111111111111111"
REVIEW = Review(action="cdd_dossier", subject="cdd-1", maker="analyst@bank.test", tenant="t1")


def _escalated_case() -> CDDCase:
    subject = Subject(
        id="subj-acme",
        name="Acme Holdings (FICTIONAL)",
        type=SubjectType.ENTITY,
        jurisdiction="SG",
        tenant="t1",
    )
    sow = SourceOfWealthNarrative(subject_id=subject.id, narrative="Business proceeds.")
    rating = RiskRating(band=RiskBand.HIGH, score=0.9, citations=())
    return CDDCase(id="cdd-subj-acme", subject=subject, sow=sow, rating=rating)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        HUMAN_REVIEW_URL_ENV,
        HUMAN_REVIEW_IAP_AUDIENCE_ENV,
        REVIEW_ROUTING_ENV,
        "CDD_S2S_TOKEN",
        "CDD_S2S_SIGNING_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CDD_PROFILE", "local")


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture what the review-kit client would put on the wire, instead of sending it."""
    requests: list[dict[str, Any]] = []

    def transport(url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> dict:
        requests.append({"url": url, "headers": dict(headers)})
        return {"review_id": f"r-{len(requests)}", "tenant": "t1", "state": "pending"}

    monkeypatch.setattr(review_kit.client, "_urllib_transport", transport)
    return requests


@pytest.fixture
def minted(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Fake the one minting helper: record each audience and return a distinct token."""
    audiences: list[str] = []

    def mint(audience: str) -> str:
        audiences.append(audience)
        return f"minted-{len(audiences)}"

    monkeypatch.setattr(_s2s, "fetch_id_token", mint)
    return audiences


# --------------------------------------------------------------------------- #
# The router
# --------------------------------------------------------------------------- #
def test_the_router_sends_a_freshly_minted_bearer_per_submission(
    monkeypatch: pytest.MonkeyPatch, sent: list[dict[str, Any]], minted: list[str]
) -> None:
    monkeypatch.setenv(HUMAN_REVIEW_URL_ENV, EDGE_URL)
    monkeypatch.setenv(HUMAN_REVIEW_IAP_AUDIENCE_ENV, AUDIENCE)
    router = PlatformReviewRouter(Settings(profile="gcp"))
    assert minted == [], "constructing the router must not spend a token"

    router.route(_escalated_case(), maker="analyst@bank.test")
    router._submit(REVIEW)

    assert minted == [AUDIENCE, AUDIENCE]
    assert [r["headers"]["Authorization"] for r in sent] == [
        "Bearer minted-1",
        "Bearer minted-2",
    ]
    assert sent[0]["url"] == f"{EDGE_URL}/v1/service/reviews"


def test_the_minted_bearer_wins_over_a_static_token(
    monkeypatch: pytest.MonkeyPatch, sent: list[dict[str, Any]], minted: list[str]
) -> None:
    monkeypatch.setenv(HUMAN_REVIEW_URL_ENV, EDGE_URL)
    monkeypatch.setenv(HUMAN_REVIEW_IAP_AUDIENCE_ENV, AUDIENCE)
    monkeypatch.setenv("CDD_S2S_TOKEN", "static-token")
    PlatformReviewRouter(Settings(profile="gcp"))._submit(REVIEW)
    assert sent[0]["headers"]["Authorization"] == "Bearer minted-1"


def test_without_an_audience_the_router_keeps_the_static_token(
    monkeypatch: pytest.MonkeyPatch, sent: list[dict[str, Any]], minted: list[str]
) -> None:
    monkeypatch.setenv(HUMAN_REVIEW_URL_ENV, "https://review.example.test")
    monkeypatch.setenv("CDD_S2S_TOKEN", "static-token")
    PlatformReviewRouter(Settings(profile="platform"))._submit(REVIEW)
    assert minted == [], "the minting helper is used only when the audience is set"
    assert sent[0]["headers"]["Authorization"] == "Bearer static-token"


def test_an_emptied_audience_refuses_at_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(HUMAN_REVIEW_IAP_AUDIENCE_ENV, "  ")
    with pytest.raises(ConfiguredEmptyError, match=HUMAN_REVIEW_IAP_AUDIENCE_ENV):
        PlatformReviewRouter(Settings(profile="gcp"))


def test_a_backend_service_path_is_refused_as_the_audience(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(HUMAN_REVIEW_IAP_AUDIENCE_ENV, BACKEND_PATH)
    with pytest.raises(ValueError, match="IAP OAuth client id, not the backend-service path"):
        PlatformReviewRouter(Settings(profile="gcp"))


# --------------------------------------------------------------------------- #
# The boot check under the IAP-fronted profile
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "named",
    [
        pytest.param({HUMAN_REVIEW_URL_ENV: EDGE_URL}, id="url-without-audience"),
        pytest.param({HUMAN_REVIEW_IAP_AUDIENCE_ENV: AUDIENCE}, id="audience-without-url"),
        pytest.param({}, id="neither"),
    ],
)
def test_gcp_routing_refuses_at_boot_without_both_variables(
    monkeypatch: pytest.MonkeyPatch, named: dict[str, str]
) -> None:
    monkeypatch.setenv("CDD_PROFILE", "gcp")
    for name, value in named.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(ConfiguredEmptyError) as refused:
        Settings.load(CONFIG)
    message = str(refused.value)
    assert HUMAN_REVIEW_URL_ENV in message
    assert HUMAN_REVIEW_IAP_AUDIENCE_ENV in message
    assert f"{REVIEW_ROUTING_ENV}=off" in message
    missing = message.split("not set:", 1)[1].split(".", 1)[0]
    for name in (HUMAN_REVIEW_URL_ENV, HUMAN_REVIEW_IAP_AUDIENCE_ENV):
        assert (name in missing) is (name not in named), (name, missing)


def test_gcp_routing_boots_with_both_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CDD_PROFILE", "gcp")
    monkeypatch.setenv(HUMAN_REVIEW_URL_ENV, EDGE_URL)
    monkeypatch.setenv(HUMAN_REVIEW_IAP_AUDIENCE_ENV, AUDIENCE)
    assert Settings.load(CONFIG).controls.review_routing is True


def test_gcp_refuses_a_backend_service_path_at_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CDD_PROFILE", "gcp")
    monkeypatch.setenv(HUMAN_REVIEW_URL_ENV, EDGE_URL)
    monkeypatch.setenv(HUMAN_REVIEW_IAP_AUDIENCE_ENV, BACKEND_PATH)
    with pytest.raises(ValueError, match=HUMAN_REVIEW_IAP_AUDIENCE_ENV):
        Settings.load(CONFIG)


def test_gcp_refuses_an_emptied_audience_at_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CDD_PROFILE", "gcp")
    monkeypatch.setenv(HUMAN_REVIEW_URL_ENV, EDGE_URL)
    monkeypatch.setenv(HUMAN_REVIEW_IAP_AUDIENCE_ENV, "")
    with pytest.raises(ConfiguredEmptyError, match=HUMAN_REVIEW_IAP_AUDIENCE_ENV):
        Settings.load(CONFIG)


def test_gcp_with_routing_stated_off_needs_neither(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CDD_PROFILE", "gcp")
    monkeypatch.setenv(REVIEW_ROUTING_ENV, "off")
    assert Settings.load(CONFIG).controls.review_routing is False


def test_platform_still_needs_only_the_console_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CDD_PROFILE", "platform")
    monkeypatch.setenv(HUMAN_REVIEW_URL_ENV, "https://review.example.test")
    assert Settings.load(CONFIG).controls.review_routing is True
