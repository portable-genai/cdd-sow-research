"""SourceOfWealthService — cited source-of-wealth narrative synthesis (SPEC §5).

Given the subject and the case's retrieved evidence passages (from the A2 governed RAG
store), generates a grounded :class:`SourceOfWealthNarrative` broken into
``WealthSource`` entries, each corroborated by document/registry/media citations, then
runs a self-critique groundedness pass that adjusts the confidence and caveats.

Pure domain code: talks only to ports and models, no Google Cloud / ADK imports.
"""

from __future__ import annotations

from typing import Any

from . import _grounded as g
from .models import (
    Citation,
    SourceOfWealthNarrative,
    Subject,
    WealthSource,
)
from .prompts import (
    _CITATION_RULES,
    SELF_CRITIQUE_SYSTEM,
    SELF_CRITIQUE_USER,
    SOW_SYSTEM,
    SOW_USER,
)

_SOW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "narrative": {"type": "string"},
        "sources": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": [
                            "employment",
                            "business_ownership",
                            "inheritance",
                            "investments",
                            "asset_sale",
                            "other",
                        ],
                    },
                    "description": {"type": "string"},
                    "est_value_band": {"type": "string"},
                    "used_source_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["kind", "description"],
            },
        },
        "confidence": {"type": "number"},
        "used_source_ids": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["narrative", "sources", "confidence", "used_source_ids"],
}

_CRITIQUE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "grounded": {"type": "boolean"},
        "confidence": {"type": "number"},
        "caveats": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["grounded", "confidence", "caveats"],
}


class SourceOfWealthService:
    """Synthesise a cited source-of-wealth narrative. Signature fixed by SPEC §5."""

    def __init__(self, llm: Any, tracer: Any) -> None:
        self._llm = llm
        self._tracer = tracer

    def build(
        self,
        subject: Subject,
        passages: list[Any],
        actor: str,
    ) -> SourceOfWealthNarrative:
        """Build a source-of-wealth narrative for ``subject`` from the evidence."""
        passage_block = g.render_passages(list(passages))
        subject_block = self._subject_block(subject)
        system = SOW_SYSTEM.format(citation_rules=_CITATION_RULES)
        user = SOW_USER.format(subject=subject_block, passages=passage_block)
        request = g.build_llm_request(
            system_instruction=system,
            user_content=user,
            model=None,  # adapter default => reasoning model gemini-3.5-flash
            response_schema=_SOW_SCHEMA,
        )
        response = self._llm.generate(request)
        g.maybe_record_usage(self._tracer, response)

        parsed = g.parse_structured(response)
        narrative = str(parsed.get("narrative") or "").strip()
        used_ids = g.as_str_list(parsed.get("used_source_ids"))
        confidence = g.clamp(parsed.get("confidence", 0.0))

        sources = self._build_sources(parsed.get("sources"), list(passages))
        citations = g.citations_for_source_ids(used_ids, list(passages))

        caveats: list[str] = []
        if not narrative:
            narrative = (
                "The available evidence does not support a confident source-of-wealth "
                "narrative for this subject. Human review is required."
            )
            confidence = min(confidence, 0.2)
            caveats.append("No grounded narrative could be synthesised from the evidence.")

        confidence, critique_caveats = self._self_critique(
            subject_block, narrative, passage_block, confidence
        )
        caveats.extend(critique_caveats)

        return SourceOfWealthNarrative(
            subject_id=subject.id,
            narrative=narrative,
            sources=sources,
            citations=citations,
            confidence=confidence,
            requires_human_review=True,
        )

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _subject_block(subject: Subject) -> str:
        dob = subject.dob_or_incorp or "unknown"
        return (
            f"id={subject.id}, name={subject.name}, type={subject.type.value}, "
            f"jurisdiction={subject.jurisdiction or 'unknown'}, dob_or_incorp={dob}"
        )

    def _build_sources(self, raw_sources: Any, passages: list[Any]) -> tuple[WealthSource, ...]:
        if not isinstance(raw_sources, list):
            return ()
        out: list[WealthSource] = []
        for raw in raw_sources:
            if not isinstance(raw, dict):
                continue
            description = str(raw.get("description") or "").strip()
            if not description:
                continue
            out.append(
                WealthSource(
                    kind=self._coerce_kind(raw.get("kind")),
                    description=description,
                    est_value_band=str(raw.get("est_value_band") or "").strip(),
                    citations=g.citations_for_source_ids(
                        g.as_str_list(raw.get("used_source_ids")), passages
                    ),
                )
            )
        return tuple(out)

    @staticmethod
    def _coerce_kind(value: Any):
        from .models import WealthSourceKind

        by_value = {k.value: k for k in WealthSourceKind}
        if isinstance(value, WealthSourceKind):
            return value
        if isinstance(value, str):
            return by_value.get(value.strip().lower(), WealthSourceKind.OTHER)
        return WealthSourceKind.OTHER

    def _self_critique(
        self,
        subject_block: str,
        narrative: str,
        passage_block: str,
        prior_confidence: float,
    ) -> tuple[float, list[str]]:
        """Second LLM pass auditing groundedness; returns (confidence, caveats)."""
        request = g.build_llm_request(
            system_instruction=SELF_CRITIQUE_SYSTEM,
            user_content=SELF_CRITIQUE_USER.format(
                subject=subject_block, narrative=narrative, passages=passage_block
            ),
            model=None,
            response_schema=_CRITIQUE_SCHEMA,
        )
        try:
            response = self._llm.generate(request)
        except Exception:  # noqa: BLE001 - critique failure must not drop the narrative
            return prior_confidence, []
        g.maybe_record_usage(self._tracer, response)

        parsed = g.parse_structured(response)
        if not parsed:
            return prior_confidence, []

        critic_conf = g.clamp(parsed.get("confidence", prior_confidence))
        grounded = bool(parsed.get("grounded", True))
        caveats = g.as_str_list(parsed.get("caveats"))

        confidence = min(prior_confidence, critic_conf)
        if not grounded:
            confidence = min(confidence, 0.4)
            if not caveats:
                caveats = ["Self-critique flagged unsupported claims; review required."]
        return confidence, caveats


def all_citations(narrative: SourceOfWealthNarrative) -> tuple[Citation, ...]:
    """Flatten the narrative-level and per-source citations (de-duplicated)."""
    seen: set[tuple[str, int | None]] = set()
    out: list[Citation] = []
    for c in (*narrative.citations, *(c for s in narrative.sources for c in s.citations)):
        key = (c.source_id, c.page)
        if key not in seen:
            seen.add(key)
            out.append(c)
    return tuple(out)
