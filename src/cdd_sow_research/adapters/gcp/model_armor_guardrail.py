"""Model Armor guardrail adapter (GuardrailPort, A1, rule R1).

Screens inbound CDD prompts and outbound dossier text through **Model Armor** on the
regional host ``modelarmor.asia-southeast1.rep.googleapis.com`` via
``sanitizeUserPrompt`` / ``sanitizeModelResponse``: prompt-injection, jailbreak,
sensitive-data and malicious-URL detection. Because B1 handles customer KYC, this screen
is mandatory in both directions (rule R1).

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
            result = client.sanitize_user_prompt(request=request)
        else:
            request = modelarmor_v1.SanitizeModelResponseRequest(
                name=self._template,
                model_response_data=modelarmor_v1.DataItem(text=text),
            )
            result = client.sanitize_model_response(request=request)
        return self._to_verdict(result, direction, text)

    @staticmethod
    def _to_verdict(result: Any, direction: Direction, text: str) -> GuardrailVerdict:
        sanitization = getattr(result, "sanitization_result", None)
        match_state = getattr(sanitization, "filter_match_state", None)
        match_name = getattr(match_state, "name", str(match_state))
        # NO_MATCH_FOUND => allowed; MATCH_FOUND => blocked.
        allowed = match_name != "MATCH_FOUND"
        findings = (
            ()
            if allowed
            else (
                GuardrailFinding(
                    category=GuardrailCategory.OTHER,
                    confidence="high",
                    detail="Model Armor filter match",
                ),
            )
        )
        return GuardrailVerdict(
            allowed=allowed,
            direction=direction,
            findings=findings,
            sanitized_text=text if allowed else None,
            reason="ok" if allowed else "blocked by Model Armor",
        )
