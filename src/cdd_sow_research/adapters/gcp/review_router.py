"""GCP/platform ReviewRouterPort: submit the routed dossier review to human-review-console via
``review-kit``.

Builds the review from the escalated dossier and submits it to the human-review-console service
intake (``POST /v1/service/reviews``), S2S-authenticated. The human-review-console base URL and S2S
credentials come from the environment (``CDD_HRZ7_URL`` / ``CDD_S2S_TOKEN`` /
``CDD_S2S_SIGNING_KEY``), set on the Cloud Run service. No cloud SDK is involved (the kit uses
stdlib ``urllib`` + the wire-compatible S2S headers), so this module imports cleanly with no GCP
SDK; it is bound under the ``gcp`` and ``platform`` profiles because it makes a real network call to
a sibling service.
"""

from __future__ import annotations

from review_kit import Review, ReviewClient

from ...config import Settings
from ...domain.models import CDDCase, PerpetualKycAssessment, UboResolution
from ...envread import required_setting
from .._review_payload import assessment_to_review, case_to_review, resolution_to_review


class PlatformReviewRouter:
    """Submit escalated CDD dossiers to human-review-console (rule R8), reusing the shared
    submission client.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def route(
        self, case: CDDCase, *, maker: str
    ) -> None:  # pragma: no cover - needs live human-review-console
        self._submit(case_to_review(case, maker=maker))

    def route_monitoring(
        self, assessment: PerpetualKycAssessment, *, maker: str
    ) -> None:  # pragma: no cover - needs live human-review-console
        """Submit a perpetual-KYC re-score to the same human-review-console intake as a dossier
        (rule R8).
        """
        self._submit(assessment_to_review(assessment, maker=maker))

    def route_ownership(
        self, resolution: UboResolution, *, maker: str
    ) -> None:  # pragma: no cover - needs live human-review-console
        """Submit a UBO-graph resolution to the same human-review-console intake (rule R8)."""
        self._submit(
            resolution_to_review(resolution, maker=maker, policy=self._settings.policy.ubo_graph)
        )

    def _submit(self, review: Review) -> None:
        base_url = required_setting("CDD_HRZ7_URL")
        client = ReviewClient(
            base_url,
            token_env="CDD_S2S_TOKEN",
            signing_key_env="CDD_S2S_SIGNING_KEY",
        )
        client.submit(review, actor="doc1-cdd-sow-research")
