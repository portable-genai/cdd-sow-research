"""Model Armor guardrail adapter (GuardrailPort, A1, rule R1).

Screens inbound CDD prompts and outbound dossier text through **Model Armor** on the
regional host ``modelarmor.asia-southeast1.rep.googleapis.com`` via
``sanitizeUserPrompt`` / ``sanitizeModelResponse``: prompt-injection, jailbreak,
sensitive-data and malicious-URL detection. Because B1 handles customer KYC, this screen
is mandatory in both directions (rule R1).

FAIL CLOSED. The verdict is ALLOWED only when ``filter_match_state`` is ``NO_MATCH_FOUND``
AND ``invocation_result`` is ``SUCCESS``, each read by the enum member's ``.name`` (``str()``
of a proto-plus ``IntEnum`` is its number on Python 3.11+). ``invocation_result`` is set
independently of the match state: ``PARTIAL`` (some filters were skipped or failed) and
``FAILURE`` (all were) arrive WITH ``NO_MATCH_FOUND``, because a skipped filter reports
``EXECUTION_SKIPPED`` and no match. Filters skip on input past their token limit, on an
unsupported language, or on a detector error, so "no match" from a screen that did not run is
refused, not passed. A missing ``sanitization_result`` or ``FILTER_MATCH_STATE_UNSPECIFIED``
blocks too. Every call carries a deadline (``model_armor.timeout_seconds``), and an API error
or timeout propagates to the caller rather than turning into a verdict.

All Google Cloud SDK imports are lazy so the on-prem / test profile imports this module
without ``google-cloud-modelarmor`` installed.
"""

from __future__ import annotations

from typing import Any

from ...config import Settings
from ...domain.models import (
    Direction,
    GuardrailCategory,
    GuardrailFinding,
    GuardrailVerdict,
)


class ModelArmorGuardrailAdapter:
    """Screen text via Model Armor on the regional endpoint (in-country)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._cfg = settings.model_armor
        self._template = (
            f"projects/{settings.project_id}/locations/{settings.region}"
            f"/templates/{self._cfg.template_id}"
        )
        self._client: Any | None = None

    def _get_client(self) -> Any:
        if self._client is None:
            from google.api_core.client_options import ClientOptions
            from google.cloud import modelarmor_v1

            self._client = modelarmor_v1.ModelArmorClient(
                client_options=ClientOptions(api_endpoint=self._cfg.host),
            )
        return self._client

    def screen(self, text: str, direction: Direction) -> GuardrailVerdict:
        """Screen ``text`` for the given direction; return a domain verdict."""
        from google.cloud import modelarmor_v1

        client = self._get_client()
        # Two request types, one variable. mypy takes the type from the FIRST branch, so the
        # OUTPUT branch contradicts it; declaring the union is what this code always meant.
        #
        # It ran correctly and stayed invisible because each request goes to the method that
        # accepts it, and the only check that could have seen the contradiction was resolving
        # `google-cloud-modelarmor` to nothing: the distribution ships no `py.typed` marker.
        # `follow_untyped_imports` is what made it visible.
        request: (
            modelarmor_v1.SanitizeUserPromptRequest | modelarmor_v1.SanitizeModelResponseRequest
        )
        if direction is Direction.INPUT:
            request = modelarmor_v1.SanitizeUserPromptRequest(
                name=self._template,
                user_prompt_data=modelarmor_v1.DataItem(text=text),
            )
            result = client.sanitize_user_prompt(request=request, timeout=self._cfg.timeout_seconds)
        else:
            request = modelarmor_v1.SanitizeModelResponseRequest(
                name=self._template,
                model_response_data=modelarmor_v1.DataItem(text=text),
            )
            result = client.sanitize_model_response(
                request=request, timeout=self._cfg.timeout_seconds
            )
        return self._to_verdict(result, direction, text)

    @staticmethod
    def _to_verdict(result: Any, direction: Direction, text: str) -> GuardrailVerdict:
        """Map a sanitize response to a verdict: allowed ONLY on a complete, clean screen.

        Complete means ``invocation_result`` is ``SUCCESS``; clean means
        ``filter_match_state`` is ``NO_MATCH_FOUND``. A match blocks however many filters ran.
        A missing or empty ``sanitization_result`` (proto-plus hands back a default message,
        ``FILTER_MATCH_STATE_UNSPECIFIED``) blocks, because a screen that returned no answer
        has not cleared the text. API errors are not caught here; they propagate.
        """
        sanitization = getattr(result, "sanitization_result", None)
        state_name = getattr(getattr(sanitization, "filter_match_state", None), "name", None)
        invocation = getattr(getattr(sanitization, "invocation_result", None), "name", None)
        if state_name == "NO_MATCH_FOUND" and invocation == "SUCCESS":
            return GuardrailVerdict(
                allowed=True,
                direction=direction,
                findings=(),
                sanitized_text=text,
                reason="ok",
            )
        if state_name == "MATCH_FOUND":
            reason = "blocked by Model Armor"
            detail = "Model Armor filter match"
        elif state_name == "NO_MATCH_FOUND":
            reason = "blocked: Model Armor returned no complete filter decision"
            detail = f"invocation_result={invocation or 'absent'}: not every filter ran"
        else:
            reason = "blocked by Model Armor"
            detail = f"Model Armor returned no usable verdict (filter_match_state={state_name})"
        return GuardrailVerdict(
            allowed=False,
            direction=direction,
            findings=(
                GuardrailFinding(
                    category=GuardrailCategory.OTHER,
                    confidence="high",
                    detail=detail,
                ),
            ),
            sanitized_text=None,
            reason=reason,
        )
