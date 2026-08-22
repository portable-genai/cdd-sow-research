"""Live LLM adapter (LLMPort) — a local Gemma model over an OpenAI-compatible server.

The ``live`` profile's generator. Everything the services ask for (the source-of-wealth
narrative, its self-critique, the risk rationale) is produced by a model running on the
operator's own machine, so the customer evidence in those prompts never leaves it.

The services request structured output by handing the adapter a JSON schema. Local
servers vary in whether they support constrained decoding, and several reject the
``response_format`` field outright, so the schema is stated in the prompt and the answer
is validated here: an unparseable first answer earns one stricter retry before the
adapter gives up and returns the raw text (the domain's tolerant parser then degrades
rather than crashing the dossier).

Grounding discipline is unchanged from the managed adapter: the model is told to cite
only the ``[source_id p.N]`` headers present in the evidence block it was given, and the
services map those ids back to real retrieved passages, so a hallucinated id cites
nothing rather than inventing provenance.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ...config import Settings
from ...domain._grounded import parse_json_object
from ...domain.models import LlmRequest, LlmResponse, TokenUsage
from ._client import LocalModelError, OpenAiCompatClient

_LOG = logging.getLogger(__name__)

_JSON_RULES = (
    "Answer with a single JSON object and nothing else: no prose, no explanation, and "
    "no markdown code fence. The object must match this JSON schema exactly, including "
    "every required property:\n{schema}"
)

_RETRY_NUDGE = (
    "Your previous answer was not valid JSON. Reply again with ONLY the JSON object "
    "matching the schema. Start your answer with { and end it with }."
)


class GemmaLocalLLMAdapter:
    """Generate with a local Gemma (or any OpenAI-compatible) model server."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        live = settings.live
        self._client = OpenAiCompatClient(live.llm_url, live.timeout_seconds)
        self._model = live.llm_model
        self._max_tokens = live.max_output_tokens

    # ------------------------------------------------------------------ #
    # LLMPort
    # ------------------------------------------------------------------ #
    def generate(self, request: LlmRequest) -> LlmResponse:
        messages = self._build_messages(request)
        model = request.model or self._model
        content, usage, model_used = self._client.chat(
            messages,
            model=model,
            temperature=request.temperature,
            max_tokens=min(request.max_output_tokens, self._max_tokens),
        )
        text = content.strip()

        if request.response_schema is not None:
            cleaned = _extract_json(text)
            if cleaned is None:
                _LOG.warning("local model returned unparseable JSON; retrying once")
                messages = [
                    *messages,
                    {"role": "assistant", "content": text[:2000]},
                    {"role": "user", "content": _RETRY_NUDGE},
                ]
                content, usage, model_used = self._client.chat(
                    messages,
                    model=model,
                    temperature=0.0,
                    max_tokens=min(request.max_output_tokens, self._max_tokens),
                )
                cleaned = _extract_json(content.strip())
            if cleaned is not None:
                text = cleaned

        return LlmResponse(
            text=text,
            usage=TokenUsage(
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
            ),
            model=model_used,
            web_citations=(),
        )

    def classify(self, text: str, labels: list[str]) -> str:
        """Single-label triage. Falls back to the first label if the server is down."""
        if not labels:
            return ""
        prompt = (
            "Classify the text below into exactly one of these labels: "
            f"{', '.join(labels)}.\nAnswer with the label only.\n\nText:\n{text[:4000]}"
        )
        try:
            content, _, _ = self._client.chat(
                [{"role": "user", "content": prompt}],
                model=self._model,
                temperature=0.0,
                max_tokens=16,
            )
        except LocalModelError:
            # Triage only routes; a server blip must not fail an assessment.
            return labels[0]
        return _match_label(content, labels)

    # ------------------------------------------------------------------ #
    # Prompt assembly
    # ------------------------------------------------------------------ #
    def _build_messages(self, request: LlmRequest) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        system = request.system_instruction or ""
        if request.response_schema is not None:
            rules = _JSON_RULES.format(schema=json.dumps(request.response_schema))
            system = f"{system}\n\n{rules}" if system else rules
        if system:
            messages.append({"role": "system", "content": system})
        for message in request.messages:
            role = "assistant" if message.role == "model" else message.role
            messages.append({"role": role, "content": message.content})
        return messages


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _extract_json(text: str) -> str | None:
    """Return ``text`` as a compact JSON object string, or None if there is none.

    Delegates to the domain's tolerant reader so "where is the JSON in this answer" is
    decided in exactly one place for every model this repo talks to, then re-serialises
    so the response carries clean JSON to the caller.
    """
    parsed = parse_json_object(text)
    return json.dumps(parsed) if parsed else None


def _match_label(content: str, labels: list[str]) -> str:
    answer = content.strip().strip(".\"'").lower()
    for label in labels:
        if answer == label.lower():
            return label
    for label in labels:  # the model often answers in a short sentence
        if label.lower() in answer:
            return label
    return labels[0]
