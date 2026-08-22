"""Remote-platform guardrail adapter — thin HTTP client to A1.

When B1 runs inside the full Gemini Enterprise Agent Platform deployment, guardrail
screening is delegated to the shared ``agent-guardrail-gateway`` service (backed by
Model Armor and DLP) rather than calling Model Armor directly. This adapter implements
:class:`GuardrailPort` by POSTing to that gateway's ``/v1/guardrail/screen`` endpoint and
parsing the response back into a domain :class:`GuardrailVerdict` (SPEC §6, A1 contract).

The base URL is read from ``HRZ_GUARDRAIL_URL`` (localhost default).
"""

from __future__ import annotations

import httpx

from ...config import Settings
from ...domain.errors import CddError
from ...domain.models import (
    Direction,
    GuardrailCategory,
    GuardrailFinding,
    GuardrailVerdict,
)
from ...domain.serialization import to_jsonable
from ...envread import setting_or_default
from . import _s2s

_DEFAULT_URL = "http://localhost:8080"
_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class RemoteGuardrailError(CddError):
    """Raised when the remote guardrail gateway returns a non-2xx response."""


class RemoteGuardrailAdapter:
    """HTTP client for the A1 ``agent-guardrail-gateway`` service."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._base_url = _s2s.validate_base_url(
            setting_or_default("HRZ_GUARDRAIL_URL", _DEFAULT_URL),
            service=type(self).__name__,
        )

    def screen(self, text: str, direction: Direction) -> GuardrailVerdict:
        """Screen inbound prompt or outbound dossier via the A1 gateway."""
        payload = {"text": text, "direction": to_jsonable(direction)}
        url = f"{self._base_url}/v1/guardrail/screen"
        try:
            response = httpx.post(
                url,
                json=payload,
                timeout=_TIMEOUT,
                headers=_s2s.headers(settings=self._settings, base_url=self._base_url),
            )
        except httpx.HTTPError as exc:
            raise RemoteGuardrailError(f"guardrail gateway request to {url} failed: {exc}") from exc
        if response.status_code // 100 != 2:
            raise RemoteGuardrailError(
                f"guardrail gateway {url} returned {response.status_code}: {response.text[:500]}"
            )
        return self._parse_verdict(response.json(), direction)

    @staticmethod
    def _parse_verdict(body: dict, fallback_direction: Direction) -> GuardrailVerdict:
        raw_direction = body.get("direction")
        direction = Direction(raw_direction) if raw_direction else fallback_direction
        findings = tuple(
            GuardrailFinding(
                category=GuardrailCategory(item.get("category", "other")),
                confidence=str(item.get("confidence", "")),
                detail=str(item.get("detail", "")),
            )
            for item in (body.get("findings") or ())
        )
        return GuardrailVerdict(
            allowed=bool(body.get("allowed", False)),
            direction=direction,
            findings=findings,
            sanitized_text=body.get("sanitized_text"),
            reason=str(body.get("reason", "")),
        )
