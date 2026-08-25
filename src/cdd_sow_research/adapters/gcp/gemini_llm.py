"""Gemini LLM adapter (LLMPort).

Wraps the unified **Google GenAI SDK** (``google-genai``) against the **Gemini
Enterprise Agent Platform** (Vertex backend) in ``asia-southeast1`` (Singapore).
Reasoning uses ``gemini-3.5-flash`` (thinking=high) for source-of-wealth and risk
synthesis; triage/classification uses ``gemini-3.1-flash-lite``. Both are pinned from
settings; the floating ADK default model and ``gemini-2.0-flash`` are never used.

The adapter maps the domain :class:`LlmRequest` onto ``client.models.generate_content``
(system instruction, temperature, max-output-tokens, a :class:`ThinkingConfig` mapped
from ``request.thinking``, and structured-output config when a response schema is
supplied), and maps ``usage_metadata`` back onto :class:`TokenUsage`.

All Google Cloud / GenAI SDK imports are lazy so the on-prem / test profile imports this
module without ``google-genai`` installed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ...config import Settings
from ...domain.models import LlmRequest, LlmResponse, ThinkingLevel, TokenUsage

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from google import genai


#: Model-id prefixes that take Gemini 3's discrete ``thinking_level``.
#:
#: Everything else takes ``thinking_budget`` (an integer, -1 for dynamic) or nothing at all.
#: The two are not interchangeable: sending ``thinking_level`` to a 2.5 model is refused with
#: "Unable to submit request because thinking_level is not supported by this model".
#:
#: This module pinned the Gemini 3 form unconditionally, which quietly contradicted the
#: settings file's own stated principle -- model ids are env-overridable "because model
#: availability is regional: a model that is GA in one region 404s in another, and a deployment
#: must be able to pin the id its own region actually serves without a fork". A deployment could
#: pin the id and still not run, because the THINKING parameter was pinned to one generation.
_DISCRETE_THINKING_PREFIXES = ("gemini-3",)


def _takes_discrete_thinking(model: str) -> bool:
    return model.strip().lower().startswith(_DISCRETE_THINKING_PREFIXES)


def _thinking_config(model: str, level: ThinkingLevel, types: Any) -> Any:
    """The thinking configuration THIS model accepts, or None when it accepts none."""

    if _takes_discrete_thinking(model):
        mapping = {
            ThinkingLevel.MINIMAL: types.ThinkingLevel.LOW,
            ThinkingLevel.LOW: types.ThinkingLevel.LOW,
            ThinkingLevel.MEDIUM: types.ThinkingLevel.HIGH,
            ThinkingLevel.HIGH: types.ThinkingLevel.HIGH,
        }
        return types.ThinkingConfig(thinking_level=mapping.get(level, types.ThinkingLevel.HIGH))
    # Gemini 2.5 takes a budget: 0 disables thinking, -1 lets the model choose. MINIMAL is the
    # only level that asks for as little thinking as possible; everything else gets dynamic.
    budget = 0 if level is ThinkingLevel.MINIMAL else -1
    return types.ThinkingConfig(thinking_budget=budget)


class GeminiLLMAdapter:
    """Generate completions and triage labels via Gemini on the Agent Platform."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._models = settings.models
        self._client: Any | None = None

    def _reasoning_model(self) -> str:
        """The model for reasoning-tier work, honouring the hard-reasoning opt-in.

        ``models.use_hard_reasoning`` is the settings switch a deployment flips to send
        reasoning-tier calls to the stronger (preview) model. A switch that selects
        nothing at all is the worst kind of config: a deployment sets it, gets the
        default model with no error and no log line, and the switch reports success
        while doing nothing. It is off by default.
        """
        if self._models.use_hard_reasoning and self._models.hard_reasoning:
            return self._models.hard_reasoning
        return self._models.reasoning

    def _get_client(self) -> genai.Client:
        if self._client is None:
            from google import genai

            self._client = genai.Client(
                vertexai=True,
                project=self._settings.project_id,
                location=self._settings.region,
            )
        return self._client

    def generate(self, request: LlmRequest) -> LlmResponse:
        """Generate a completion for ``request`` using the configured model."""
        from google.genai import types

        client = self._get_client()
        model = request.model or self._reasoning_model()
        contents = self._to_contents(request)
        # The SAME model string the request will carry: a thinking parameter chosen for a
        # different model than the one being called is the defect this argument removes.
        config = self._build_config(request, types, model)

        response = client.models.generate_content(model=model, contents=contents, config=config)
        return LlmResponse(
            text=getattr(response, "text", "") or "",
            usage=self._map_usage(getattr(response, "usage_metadata", None)),
            model=model,
        )

    def classify(self, text: str, labels: list[str]) -> str:
        """Cheap single-label classification using the triage-tier model."""
        from google.genai import types

        client = self._get_client()
        prompt = (
            "Classify the text into exactly one of these labels: "
            f"{', '.join(labels)}.\n"
            "Reply with the single label only, no punctuation or explanation.\n\n"
            f"Text:\n{text}"
        )
        response = client.models.generate_content(
            model=self._models.triage,
            # A single Content, not a one-element list: the SDK accepts both, and the
            # bare form types cleanly (list[Content] is invariant against the SDK union).
            contents=types.Content(role="user", parts=[types.Part.from_text(text=prompt)]),
            config=types.GenerateContentConfig(
                temperature=0.0,
                max_output_tokens=16,
                thinking_config=_thinking_config(self._models.triage, ThinkingLevel.MINIMAL, types),
            ),
        )
        raw = (getattr(response, "text", "") or "").strip()
        return self._match_label(raw, labels)

    def _to_contents(self, request: LlmRequest) -> list[Any]:
        from google.genai import types

        contents: list[Any] = []
        for message in request.messages:
            role = "model" if message.role == "model" else "user"
            contents.append(
                types.Content(role=role, parts=[types.Part.from_text(text=message.content)])
            )
        return contents

    def _build_config(self, request: LlmRequest, types: Any, model: str) -> Any:
        kwargs: dict[str, Any] = {
            "temperature": request.temperature,
            "max_output_tokens": request.max_output_tokens,
            "thinking_config": _thinking_config(model, request.thinking, types),
        }
        if request.system_instruction:
            kwargs["system_instruction"] = request.system_instruction
        if request.response_schema is not None:
            kwargs["response_mime_type"] = "application/json"
            kwargs["response_schema"] = request.response_schema
        return types.GenerateContentConfig(**kwargs)

    @staticmethod
    def _map_usage(usage_metadata: Any) -> TokenUsage:
        if usage_metadata is None:
            return TokenUsage()
        return TokenUsage(
            input_tokens=int(getattr(usage_metadata, "prompt_token_count", 0) or 0),
            output_tokens=int(getattr(usage_metadata, "candidates_token_count", 0) or 0),
            thinking_tokens=int(getattr(usage_metadata, "thoughts_token_count", 0) or 0),
        )

    @staticmethod
    def _match_label(raw: str, labels: list[str]) -> str:
        if not labels:
            return raw
        lowered = raw.lower()
        for label in labels:
            if label.lower() == lowered:
                return label
        for label in labels:
            if label.lower() in lowered:
                return label
        return labels[0]
