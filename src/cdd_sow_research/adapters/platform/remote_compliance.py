"""Compliance client that asks `compliance-advisory` over HTTP.

`cdd-sow-research` checks each dossier's rating against regulatory CDD/AML expectations by
asking `compliance-advisory`, the grounded compliance assistant. This adapter implements
:class:`ComplianceClientPort` by POSTing to its ``/ask`` endpoint and projecting the answer onto
a domain :class:`ComplianceAnswer`, citations included.

It is bound under ``gcp``, ``live`` and ``platform``: every profile other than the offline gate
asks the real service. ``RSK_COMPLIANCE_URL`` names that service and is read in three states
with no default. Unset and emptied both refuse at construction: a networked profile names the
service it asks rather than inheriting a localhost guess. On a managed profile each request
carries a Google-signed ID token minted for the service's origin (see :mod:`._s2s`).
"""

from __future__ import annotations

import httpx

from ...config import Settings
from ...domain.errors import CddError
from ...domain.models import Citation, ComplianceAnswer, SourceType
from ...envread import required_setting
from . import _s2s

#: The one environment variable that names the compliance-advisory base URL.
URL_ENV = "RSK_COMPLIANCE_URL"
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
                headers=_s2s.headers(settings=self._settings, base_url=self._base_url),
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
