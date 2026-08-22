"""Remote-platform redaction adapter — thin HTTP client to A1.

Delegates PII de-identification to the shared ``agent-guardrail-gateway`` service's
``/v1/redact`` endpoint (backed by DLP) rather than calling DLP directly. This adapter
implements :class:`PIIRedactionPort` and is mandatory for B1 (rule R1): customer KYC PII
is removed at the boundary before any model, index or audit call (P-04).

The base URL is read from ``HRZ_GUARDRAIL_URL`` (localhost default).
"""

from __future__ import annotations

import httpx

from ...config import Settings
from ...domain.errors import CddError
from ...domain.models import RedactionFinding, RedactionResult
from ...envread import setting_or_default
from . import _s2s

_DEFAULT_URL = "http://localhost:8080"
_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class RemoteRedactionError(CddError):
    """Raised when the remote guardrail gateway returns a non-2xx response."""


class RemoteRedactionAdapter:
    """HTTP client for the A1 ``agent-guardrail-gateway`` redaction endpoint."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._base_url = _s2s.validate_base_url(
            setting_or_default("HRZ_GUARDRAIL_URL", _DEFAULT_URL),
            service=type(self).__name__,
        )

    def redact(self, text: str) -> RedactionResult:
        """De-identify ``text`` via the A1 gateway before it reaches a model or sink."""
        url = f"{self._base_url}/v1/redact"
        try:
            response = httpx.post(
                url,
                json={"text": text},
                timeout=_TIMEOUT,
                headers=_s2s.headers(settings=self._settings, base_url=self._base_url),
            )
        except httpx.HTTPError as exc:
            raise RemoteRedactionError(f"redact request to {url} failed: {exc}") from exc
        if response.status_code // 100 != 2:
            raise RemoteRedactionError(
                f"redact {url} returned {response.status_code}: {response.text[:500]}"
            )
        body = response.json()
        findings = tuple(
            RedactionFinding(
                info_type=str(item.get("info_type", "")),
                count=int(item.get("count", 1) or 1),
            )
            for item in (body.get("findings") or ())
        )
        return RedactionResult(text=str(body.get("text", text)), findings=findings)
