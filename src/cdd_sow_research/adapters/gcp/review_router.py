"""GCP/platform ReviewRouterPort: submit the routed dossier review to human-review-console via
``review-kit``.

Builds the review from the escalated dossier and submits it to the human-review-console service
intake (``POST /v1/service/reviews``). The base URL comes from ``HUMAN_REVIEW_URL`` and the signed
actor from ``CDD_S2S_SIGNING_KEY``; the bearer depends on how the console is reached:

* **Through the portal's IAP edge** (``gcp``): the deployed console is an embedded app behind
  `journey-portal`, so ``HUMAN_REVIEW_URL`` is its edge path
  (``https://<edge-host>/apps/human-review-console/api``) and the edge accepts only a
  Google-signed ID token minted for the IAP OAuth client id, named by
  ``HUMAN_REVIEW_IAP_AUDIENCE``. The router mints one per submission with this service's
  workload identity (:func:`..platform._s2s.fetch_id_token`, the same helper the compliance leg
  uses), so an expiring token is never reused. The console authenticates this service from the
  IAP assertion the edge forwards, not from the portal's bearer that replaces this one.
* **Directly** (audience unset): the static ``CDD_S2S_TOKEN`` bearer, as before.

``HUMAN_REVIEW_IAP_AUDIENCE`` is read in three states: unset keeps the static bearer, emptied
refuses at construction, and a backend-service path pasted where the client id belongs refuses
by name. Under ``gcp`` the boot check in :mod:`cdd_sow_research.config` requires it beside the
URL while routing is on. The kit itself uses stdlib ``urllib``; ``google-auth`` is imported only
when a token is minted.
"""

from __future__ import annotations

from review_kit import Review, ReviewClient

from ...config import HUMAN_REVIEW_IAP_AUDIENCE_ENV, Settings, iap_audience_or_refuse
from ...domain.models import CDDCase, PerpetualKycAssessment, UboResolution
from ...envread import optional_setting, required_setting
from .._review_payload import assessment_to_review, case_to_review, resolution_to_review
from ..platform import _s2s


class PlatformReviewRouter:
    """Submit escalated CDD dossiers to human-review-console (rule R8), reusing the shared
    submission client.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        audience = optional_setting(HUMAN_REVIEW_IAP_AUDIENCE_ENV)
        self._audience = (
            None
            if audience is None
            else iap_audience_or_refuse(HUMAN_REVIEW_IAP_AUDIENCE_ENV, audience)
        )

    def route(self, case: CDDCase, *, maker: str) -> None:
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
        self._client().submit(review, actor="doc1-cdd-sow-research")

    def _client(self) -> ReviewClient:
        base_url = required_setting("HUMAN_REVIEW_URL")
        audience = self._audience
        if audience is None:
            return ReviewClient(
                base_url,
                token_env="CDD_S2S_TOKEN",
                signing_key_env="CDD_S2S_SIGNING_KEY",
            )
        return ReviewClient(
            base_url,
            token_env="CDD_S2S_TOKEN",
            signing_key_env="CDD_S2S_SIGNING_KEY",
            bearer_provider=lambda: _s2s.fetch_id_token(audience),
        )
