"""Local LLM adapter (LLMPort) — a deterministic, schema-driven generator.

The ``local`` profile's stand-in for **Gemini**: no model, no network, fully
reproducible. It reads ``request.response_schema`` (the JSON schema the calling service
asks for) and emits a deterministic JSON object whose keys match it, including
``used_source_ids`` mapped from the ``[source_id p.N]`` headers present in the rendered
evidence block, plus a plausible ``classify``. There is no Google emulator for Gemini,
so this path is unconditional.

The schema-driven ``FakeLLM`` is a real, registered adapter rather than a test fixture, so
the in-memory implementation lives once under ``adapters/local`` and drives both the offline
tests and the CLI. The
source-of-wealth schema nests a ``sources`` array, the risk schema nests a ``factors``
array, and the self-critique schema is a flat object; the adapter inspects the declared
property set and emits the right shape.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ...config import Settings
from ...domain.models import (
    LlmRequest,
    LlmResponse,
    TokenUsage,
)

# The rendered evidence block keys each source with ``[source_id p.N]`` headers; recover
# the ids the service actually grounded on so the answer cites only retrieved sources.
_SOURCE_HEADER_RE = re.compile(r"\[([a-z0-9][a-z0-9\-]*?)(?:\s+p\.[^\]]+)?\]")


def _schema_properties(schema: dict | None) -> dict[str, Any]:
    if not schema:
        return {}
    props = schema.get("properties")
    return props if isinstance(props, dict) else {}


#: The fact line only the UBO narrator sends. See ``_body_for_schema``.
_UBO_FACT_MARKER = "control_basis:"


class LocalDeterministicLLMAdapter:
    """Deterministic LLM whose ``generate`` returns JSON matching the request schema."""

    REASONING_MODEL = "gemini-3.5-flash"
    TRIAGE_MODEL = "gemini-3.1-flash-lite"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._reasoning_model = settings.models.reasoning or self.REASONING_MODEL
        self._triage_model = settings.models.triage or self.TRIAGE_MODEL

    # ------------------------------------------------------------------ #
    # LLMPort
    # ------------------------------------------------------------------ #
    def generate(self, request: LlmRequest) -> LlmResponse:
        source_ids = self._source_ids_from_request(request)
        body = self._body_for_schema(
            request.response_schema, source_ids, self._user_content(request)
        )
        return LlmResponse(
            text=json.dumps(body),
            usage=TokenUsage(input_tokens=128, output_tokens=64, thinking_tokens=32),
            model=request.model or self._reasoning_model,
            web_citations=(),
            raw=body,
        )

    def classify(self, text: str, labels: list[str]) -> str:
        # Deterministic triage: first label (the services only use this for routing).
        return labels[0] if labels else ""

    # ------------------------------------------------------------------ #
    # Schema-driven body
    # ------------------------------------------------------------------ #
    @staticmethod
    def _user_content(request: LlmRequest) -> str:
        for message in reversed(request.messages):
            if message.role == "user":
                return message.content
        return ""

    def _source_ids_from_request(self, request: LlmRequest) -> list[str]:
        user = self._user_content(request)
        seen: list[str] = []
        for sid in _SOURCE_HEADER_RE.findall(user):
            if sid not in seen:
                seen.append(sid)
        return seen

    def _body_for_schema(
        self, schema: dict | None, source_ids: list[str], user: str = ""
    ) -> dict[str, Any]:
        props = _schema_properties(schema)
        sid = list(source_ids)
        if "sources" in props:  # source-of-wealth narrative
            return {
                "narrative": (
                    "The subject's wealth derives principally from a majority shareholding "
                    "in a profitable logistics business, with a one-off gain from an earlier "
                    "residential property sale."
                ),
                "sources": [
                    {
                        "kind": "business_ownership",
                        "description": "Majority shareholding in a logistics business.",
                        "est_value_band": "USD 1m-5m",
                        "used_source_ids": sid,
                    },
                    {
                        "kind": "asset_sale",
                        "description": "One-off gain from a residential property sale.",
                        "est_value_band": "USD 100k-1m",
                        "used_source_ids": sid,
                    },
                ],
                "confidence": 0.88 if sid else 0.2,
                "used_source_ids": sid,
            }
        if "factors" in props:  # risk rating
            return {
                "band": "medium",
                "score": 0.45,
                "factors": [
                    {
                        "name": "business_ownership_transparency",
                        "weight": 0.4,
                        "present": True,
                        "detail": "Ownership is documented in the registry extract.",
                        "used_source_ids": sid,
                    }
                ],
                "rationale": "Wealth is well documented and ownership is transparent.",
                "used_source_ids": sid,
            }
        if set(props) == {"narrative"} and _UBO_FACT_MARKER in user:  # UBO-graph narration
            # Both narrators declare the same one-string schema, so the schema alone
            # cannot tell them apart; the FACTS block does. Getting this wrong is not
            # cosmetic: the offline demo would caption an ownership resolution with a
            # sentence about a re-score that never happened.
            return {
                "narrative": (
                    "The beneficial-ownership structure behind this subject was resolved "
                    "from cited corporate-registry layers, and the effective ownership "
                    "percentages shown were computed deterministically from those layers. "
                    "Any structural indicators listed are prompts to verify, not findings "
                    "of wrongdoing. No action has been taken: this resolution requires "
                    "human review and a reviewer's confirmation."
                )
            }
        if set(props) == {"narrative"}:  # perpetual-KYC queue narration
            # Deliberately number-free: the offline narrator restates that a change was
            # detected and that a human must dispose of it, and leaves every figure to
            # the deterministic engine that already computed it.
            return {
                "narrative": (
                    "Perpetual-KYC monitoring detected a change against the stored "
                    "baseline for this relationship and re-scored it using the bank's "
                    "policy weights. The queued reasons list what moved and the evidence "
                    "behind each. No action has been taken: this item requires human "
                    "review and a checker's disposition."
                )
            }
        # Flat object (self-critique).
        return {"grounded": True, "confidence": 0.86, "caveats": []}
