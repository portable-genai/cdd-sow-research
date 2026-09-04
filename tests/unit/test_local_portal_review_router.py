"""The local journey bridge persists and directly delivers CDD escalations
to human-review-console.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from review_kit import ReviewClientError

from cdd_sow_research.adapters.local.review_router import LocalReviewRouter
from cdd_sow_research.config import Settings
from cdd_sow_research.domain.models import (
    CDDCase,
    RiskBand,
    RiskRating,
    SourceOfWealthNarrative,
    Subject,
    SubjectType,
)


def _completed_case() -> CDDCase:
    subject = Subject(
        id="subj-acme-holdings",
        name="Acme Holdings Pte Ltd (FICTIONAL)",
        type=SubjectType.ENTITY,
        jurisdiction="SG",
        tenant="demo-bank",
    )
    return CDDCase(
        id="cdd-subj-acme-holdings",
        subject=subject,
        sow=SourceOfWealthNarrative(subject_id=subject.id, narrative="Fictional trading proceeds."),
        rating=RiskRating(band=RiskBand.MEDIUM, score=0.5),
    )


def test_local_router_persists_then_submits_to_service_intake(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("CDD_LOCAL_REVIEW_OUTBOX", str(tmp_path / "outbox.db"))
    monkeypatch.setenv("CDD_HRZ7_URL", "http://127.0.0.1:8087")
    monkeypatch.setenv("CDD_S2S_TOKEN", "synthetic-local-secret")
    with patch(
        "cdd_sow_research.adapters.local.review_router.ReviewClient.submit",
        return_value=object(),
    ) as submit:
        router = LocalReviewRouter(Settings())
        router.route(_completed_case(), maker="analyst@bank.test")

    # Actor is a service, never the portal's user identity.
    assert submit.call_args.kwargs["actor"] == "doc1-cdd-sow-research"
    submitted_review = submit.call_args.args[0]
    assert submitted_review.maker == "analyst@bank.test"
    assert submitted_review.tenant == "demo-bank"
    assert (
        submitted_review.source_key
        == "cdd-sow-research:demo-bank:cdd-subj-acme-holdings:cdd_dossier"
    )
    assert router.outbox.pending() == ()


def test_local_router_retries_a_record_left_by_an_unavailable_console(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("CDD_LOCAL_REVIEW_OUTBOX", str(tmp_path / "outbox.db"))
    monkeypatch.setenv("CDD_HRZ7_URL", "http://127.0.0.1:8087")
    monkeypatch.setenv("CDD_S2S_TOKEN", "synthetic-local-secret")
    with patch(
        "cdd_sow_research.adapters.local.review_router.ReviewClient.submit",
        side_effect=ReviewClientError("human-review-console down"),
    ):
        router = LocalReviewRouter(Settings())
        router.route(_completed_case(), maker="analyst@bank.test")
    assert len(router.outbox.pending()) == 1

    with patch(
        "cdd_sow_research.adapters.local.review_router.ReviewClient.submit",
        return_value=object(),
    ):
        restarted = LocalReviewRouter(Settings())
    assert restarted.outbox.pending() == ()


def test_local_router_rejects_non_loopback_service_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CDD_LOCAL_REVIEW_OUTBOX", ":memory:")
    monkeypatch.setenv("CDD_HRZ7_URL", "http://review.example.test")
    monkeypatch.setenv("CDD_S2S_TOKEN", "synthetic-local-secret")
    with pytest.raises(ValueError, match="https outside loopback"):
        LocalReviewRouter(Settings())


def test_local_router_requires_s2s_secret_when_delivery_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CDD_LOCAL_REVIEW_OUTBOX", ":memory:")
    monkeypatch.setenv("CDD_HRZ7_URL", "http://127.0.0.1:8087")
    monkeypatch.delenv("CDD_S2S_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="CDD_S2S_TOKEN"):
        LocalReviewRouter(Settings())
