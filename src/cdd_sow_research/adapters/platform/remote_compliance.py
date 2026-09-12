"""Compliance client that asks `compliance-advisory` over HTTP.

`cdd-sow-research` checks each dossier's rating against regulatory CDD/AML expectations by
asking `compliance-advisory`, the grounded compliance assistant. This adapter implements
:class:`ComplianceClientPort` by POSTing to its ``/ask`` endpoint and projecting the answer onto
a domain :class:`ComplianceAnswer`, citations included.

It is bound under ``gcp``, ``live`` and ``platform``: every profile other than the offline gate
asks the real service. ``RSK_COMPLIANCE_URL`` names that service and is read in three states
with no default. Unset and emptied both refuse at construction: a networked profile names the
service it asks rather than inheriting a localhost guess.

**The deployed call goes through the portal's IAP edge, not to the sibling service.** Every
route a deployed `compliance-advisory` answers on is an embedded app behind `journey-portal`:
its API takes internal traffic only, only the portal's own service account may invoke it, this
service has no VPC egress, and its managed identity accepts an IAP assertion and nothing else.
So a direct service-to-service call cannot succeed in any of four independent ways, and
``RSK_COMPLIANCE_URL`` is the portal edge path for that app,
``https://<rm-domain>/apps/compliance-advisory/api``. The base URL therefore carries a path
prefix, which survives validation, and ``/ask`` is appended inside the mount.

IAP accepts one bearer audience: the deployment's **IAP OAuth client id**. It is named by
``RSK_COMPLIANCE_IAP_AUDIENCE``, required under ``gcp`` and refused when emptied, because a
token minted for the edge's origin, or for the backend-service path IAP compares its own
assertion against, is rejected at the edge with nothing in this process able to tell why. This
is the same audience `journey-portal`'s ``make e2e-gcp`` and an app's ``make verify-deployed``
present as the `portal-e2e@` service account; only the minting differs (``gcloud`` impersonation
there, this service's own workload identity here).

``platform`` is unchanged by any of that: it still means a thin delegate to a sibling contract
reached directly, so it mints for the origin like the five platform adapters beside it, and the
audience is optional there. ``live`` calls a `compliance-advisory` the launcher started on
loopback, which runs no IAP, so it sends no token at all.
"""

from __future__ import annotations

import httpx

from ...config import Settings
from ...domain.errors import CddError
from ...domain.models import Citation, ComplianceAnswer, SourceType
from ...envread import optional_setting, required_setting
from . import _s2s

#: The one environment variable that names the compliance-advisory base URL. Under ``gcp`` it is
#: the portal edge path for the embedded app, so it carries a path prefix.
URL_ENV = "RSK_COMPLIANCE_URL"
#: The one environment variable that names the audience the edge accepts: the IAP OAuth client id.
AUDIENCE_ENV = "RSK_COMPLIANCE_IAP_AUDIENCE"
#: The profile whose receiver is behind IAP, and so the profile the audience is required under.
_IAP_PROFILE = "gcp"
_TIMEOUT = httpx.Timeout(30.0, connect=5.0)


class RemoteComplianceError(CddError):
    """Raised when compliance-advisory cannot be reached or answers with a non-2xx status."""


class RemoteComplianceAdapter:
    """HTTP client for the `compliance-advisory` ``/ask`` endpoint."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._base_url = _s2s.validate_base_url(
            required_setting(URL_ENV),
            service=type(self).__name__,
        )
        self._audience = self._resolve_audience(settings)

    @staticmethod
    def _resolve_audience(settings: Settings) -> str:
        """Resolve the bearer audience in three states, required only where IAP is the receiver.

        Under ``gcp`` the receiver is the portal's IAP edge, so an unnamed audience cannot be
        guessed and an emptied one is an expressed intent that names nothing: both refuse at
        construction and name the variable. Everywhere else the audience is absent and
        :func:`._s2s.headers` falls back to the receiver's own origin, which is what a direct
        Cloud Run call accepts.
        """
        if settings.profile == _IAP_PROFILE:
            return _audience_or_refuse(required_setting(AUDIENCE_ENV))
        configured = optional_setting(AUDIENCE_ENV)
        return _audience_or_refuse(configured) if configured else ""

    def check(self, question: str, actor: str) -> ComplianceAnswer:
        """Ask compliance-advisory a regulatory CDD/AML question and return its cited answer.

        ``actor`` is not sent. compliance-advisory resolves its principal from the verified
        caller and ignores any actor in the body, so putting one on the wire would only suggest
        that the receiver trusts it.
        """
        url = f"{self._base_url}/ask"
        payload = {"question": question, "filters": None}
        try:
            response = httpx.post(
                url,
                json=payload,
                timeout=_TIMEOUT,
                headers=_s2s.headers(
                    settings=self._settings,
                    base_url=self._base_url,
                    audience=self._audience,
                ),
            )
        except httpx.HTTPError as exc:
            raise RemoteComplianceError(f"compliance request to {url} failed: {exc}") from exc
        if response.status_code // 100 != 2:
            raise RemoteComplianceError(
                f"compliance {url} returned {response.status_code}: {response.text[:500]}"
            )
        return self._parse(question, response.json())

    @staticmethod
    def _parse(question: str, body: dict) -> ComplianceAnswer:
        citations = tuple(
            Citation(
                source_id=str(item.get("source_id", "")),
                source_type=SourceType.REGULATION,
                title=str(item.get("title", "")),
                url=str(item.get("url", "")),
                page=item.get("page"),
                snippet=str(item.get("snippet", "")),
                score=item.get("score"),
            )
            for item in (body.get("citations") or ())
        )
        return ComplianceAnswer(
            question=str(body.get("question", question)),
            answer=str(body.get("answer", "")),
            citations=citations,
            requires_human_review=bool(body.get("requires_human_review", True)),
            confidence=float(body.get("confidence", 0.0) or 0.0),
        )


def _audience_or_refuse(value: str) -> str:
    """Refuse the one wrong audience an operator is most likely to paste.

    IAP compares its OWN assertion against the backend-service path
    (``/projects/<n>/global/backendServices/<id>``), and that path is NOT a bearer audience: a
    token minted for it is refused at the edge, and this process only ever sees the refusal,
    never the reason. The two values live side by side in a deployment record, so catching the
    mix-up here is the difference between a named configuration error and an unexplained 401.
    """
    if value.startswith("/projects/") or "/backendServices/" in value:
        raise ValueError(
            f"{AUDIENCE_ENV} must be the IAP OAuth client id, not the backend-service path "
            f"{value!r}: IAP compares that path against its own assertion and refuses it as a "
            "bearer audience."
        )
    return value
