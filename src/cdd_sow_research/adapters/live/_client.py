"""Minimal OpenAI-compatible chat client for a locally hosted model server.

Shared by the ``live`` LLM adapter and the ``live`` extractor (page-image transcription).
Targets the chat-completions shape that MLX's server, Ollama, vLLM and llama.cpp all
speak, and deliberately sends only the fields every one of them accepts: unknown keys
such as ``response_format`` are rejected outright by some of these servers, so JSON is
requested in the prompt and validated by the caller instead.

Loopback by default and no credentials: the whole point of this profile is that customer
documents are processed by a model running on the operator's own machine.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

# Servers disagree on the usage field names (OpenAI: prompt_tokens/completion_tokens;
# MLX: input_tokens/output_tokens). Read both so token accounting is never silently zero.
_INPUT_KEYS = ("prompt_tokens", "input_tokens")
_OUTPUT_KEYS = ("completion_tokens", "output_tokens")


class LocalModelError(RuntimeError):
    """The local model server was unreachable or returned an unusable response."""


class OpenAiCompatClient:
    """Call a local OpenAI-compatible ``/chat/completions`` endpoint."""

    def __init__(self, url: str, timeout_seconds: float = 240.0) -> None:
        self._url = url
        self._timeout = timeout_seconds

    @property
    def url(self) -> str:
        return self._url

    def chat(
        self,
        messages: list[dict[str, Any]],
        model: str,
        temperature: float = 0.2,
        max_tokens: int = 2048,
    ) -> tuple[str, dict[str, int], str]:
        """Return ``(content, usage, model_used)`` for one chat completion.

        Raises :class:`LocalModelError` when the server is unreachable or the response
        is not a chat completion. The caller decides whether that is fatal: generation
        is, page transcription is not.
        """
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        try:
            response = httpx.post(self._url, json=payload, timeout=self._timeout)
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPError as exc:
            raise LocalModelError(
                f"local model server at {self._url} is unreachable or failed: {exc}. "
                "Start it (for example an MLX or Ollama server) and check "
                "CDD_LIVE_LLM_URL."
            ) from exc
        except json.JSONDecodeError as exc:
            raise LocalModelError(f"local model server returned non-JSON: {exc}") from exc

        choices = body.get("choices") or []
        if not choices:
            raise LocalModelError(f"local model server returned no choices: {body!r:.200}")
        content = (choices[0].get("message") or {}).get("content") or ""
        raw_usage = body.get("usage") or {}
        usage = {
            "input_tokens": _first_int(raw_usage, _INPUT_KEYS),
            "output_tokens": _first_int(raw_usage, _OUTPUT_KEYS),
        }
        return str(content), usage, str(body.get("model") or model)

    def health(self) -> bool:
        """True when the server answers at all (used by the readiness probe)."""
        base = self._url.rsplit("/", 2)[0] if "/" in self._url else self._url
        for candidate in (f"{base}/models", f"{base}/health"):
            try:
                if httpx.get(candidate, timeout=5.0).status_code == 200:
                    return True
            except httpx.HTTPError:
                continue
        return False


def _first_int(raw: dict[str, Any], keys: tuple[str, ...]) -> int:
    for key in keys:
        if key in raw:
            try:
                return int(raw[key])
            except (TypeError, ValueError):
                return 0
    return 0


def text_part(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def image_part(png_bytes: bytes) -> dict[str, Any]:
    """An inline base64 image content part (the shape every VLM server accepts)."""
    import base64

    encoded = base64.b64encode(png_bytes).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}}
