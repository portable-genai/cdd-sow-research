"""Remote-platform compliance adapter — thin HTTP client to C1.

B1 checks its source-of-wealth / risk reasoning against regulatory CDD/AML expectations
(MAS / HKMA / APRA / FSA) by asking **C1**, the grounded compliance assistant
(``compliance-advisory``). This adapter implements :class:`ComplianceClientPort` by
POSTing to C1's ``/ask`` endpoint and projecting the AnswerResponse onto a domain
:class:`ComplianceAnswer` (SPEC §6, C1 contract).

The base URL is read from ``RSK_COMPLIANCE_URL`` with a localhost default.
"""

from __future__ import annotations

import httpx

from ...config import Settings
from ...domain.errors import CddError
from ...domain.models import Citation, SourceType
from ...envread import setting_or_default
from ...ports.compliance import ComplianceAnswer
from . import _s2s

_DEFAULT_URL = "http://localhost:8080"
_TIMEOUT = httpx.Timeout(30.0, connect=5.0)


class RemoteComplianceError(CddError):
    """Raised when the C1 compliance service returns a non-2xx response."""


class RemoteComplianceAdapter:
    """HTTP client for the C1 ``compliance-advisory`` /ask endpoint."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._base_url = _s2s.validate_base_url(
            setting_or_default("RSK_COMPLIANCE_URL", _DEFAULT_URL),
            service=type(self).__name__,
        )

    def check(self, question: str, actor: str) -> ComplianceAnswer:
        """Ask C1 a regulatory CDD/AML question and return its cited answer."""
        url = f"{self._base_url}/ask"
        payload = {"question": question, "actor": actor, "filters": None}
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
