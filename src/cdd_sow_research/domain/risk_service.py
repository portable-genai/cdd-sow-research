"""RiskRatingService — weighted customer-risk rating (SPEC §5).

Given the subject, the source-of-wealth narrative, the adverse-media findings, and the
ownership picture, plus the case evidence passages, the LLM produces a grounded
:class:`RiskRating` with weighted :class:`RiskFactor` entries and a written rationale.
The domain then deterministically *raises* the band when a sanctions/terrorism
adverse-media hit or a PEP beneficial owner is present, so a hard signal can never be
softened by the model.

Pure domain code: talks only to ports and models, no Google Cloud / ADK imports.
"""

from __future__ import annotations

from typing import Any

from . import _grounded as g
from .models import (
    AdverseMediaCategory,
    AdverseMediaFinding,
    OwnershipSummary,
    RiskBand,
    RiskFactor,
    RiskRating,
    Severity,
    SourceOfWealthNarrative,
    Subject,
)
from .prompts import _CITATION_RULES, RISK_SYSTEM, RISK_USER

_RISK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "band": {
            "type": "string",
            "enum": ["low", "medium", "high", "prohibited"],
        },
        "score": {"type": "number"},
        "factors": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "weight": {"type": "number"},
                    "present": {"type": "boolean"},
                    "detail": {"type": "string"},
                    "used_source_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name", "weight", "present"],
            },
        },
        "rationale": {"type": "string"},
        "used_source_ids": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["band", "score", "factors", "rationale", "used_source_ids"],
}


class RiskRatingService:
    """Assign a weighted, grounded risk rating. Signature fixed by SPEC §5."""

    def __init__(self, llm: Any, tracer: Any) -> None:
        self._llm = llm
        self._tracer = tracer

    def rate(
        self,
        subject: Subject,
        sow: SourceOfWealthNarrative,
        adverse_media: tuple[AdverseMediaFinding, ...],
        ownership: OwnershipSummary | None,
        passages: list[Any],
        actor: str,
    ) -> RiskRating:
        """Assign a risk rating for ``subject`` grounded in the case evidence."""
        signals = self._signals_block(sow, adverse_media, ownership)
        subject_block = (
            f"id={subject.id}, name={subject.name}, type={subject.type.value}, "
            f"jurisdiction={subject.jurisdiction or 'unknown'}"
        )
        passage_block = g.render_passages(list(passages))
        system = RISK_SYSTEM.format(citation_rules=_CITATION_RULES)
        user = RISK_USER.format(subject=subject_block, signals=signals, passages=passage_block)
        request = g.build_llm_request(
            system_instruction=system,
            user_content=user,
            model=None,
            response_schema=_RISK_SCHEMA,
        )
        response = self._llm.generate(request)
        g.maybe_record_usage(self._tracer, response)

        parsed = g.parse_structured(response)
        band = g.coerce_risk_band(parsed.get("band"))
        score = g.clamp(parsed.get("score", 0.0))
        factors = self._build_factors(parsed.get("factors"), list(passages))
        rationale = str(parsed.get("rationale") or "").strip()
        citations = g.citations_for_source_ids(
            g.as_str_list(parsed.get("used_source_ids")), list(passages)
        )

        # Deterministically raise the band on hard signals (never soften the model).
        band, rationale = self._apply_hard_signals(band, rationale, adverse_media, ownership)

        return RiskRating(
            band=band,
            score=score,
            factors=factors,
            rationale=rationale,
            citations=citations,
            requires_human_review=True,
        )

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _signals_block(
        sow: SourceOfWealthNarrative,
        adverse_media: tuple[AdverseMediaFinding, ...],
        ownership: OwnershipSummary | None,
    ) -> str:
        sow_kinds = ", ".join(str(s.kind) for s in sow.sources) or "none"
        media = (
            "; ".join(f"{f.category.value}/{f.severity.value}: {f.headline}" for f in adverse_media)
            or "none"
        )
        peps = (
            ", ".join(o.name for o in (ownership.owners if ownership else ()) if o.is_pep) or "none"
        )
        return (
            f"source_of_wealth_kinds=[{sow_kinds}]\n"
            f"sow_confidence={sow.confidence:.2f}\n"
            f"adverse_media=[{media}]\n"
            f"pep_owners=[{peps}]"
        )

    def _build_factors(self, raw_factors: Any, passages: list[Any]) -> tuple[RiskFactor, ...]:
        if not isinstance(raw_factors, list):
            return ()
        out: list[RiskFactor] = []
        for raw in raw_factors:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or "").strip()
            if not name:
                continue
            out.append(
                RiskFactor(
                    name=name,
                    weight=g.clamp(raw.get("weight", 0.0)),
                    present=bool(raw.get("present", False)),
                    detail=str(raw.get("detail") or "").strip(),
                    citations=g.citations_for_source_ids(
                        g.as_str_list(raw.get("used_source_ids")), passages
                    ),
                )
            )
        return tuple(out)

    @staticmethod
    def _apply_hard_signals(
        band: RiskBand,
        rationale: str,
        adverse_media: tuple[AdverseMediaFinding, ...],
        ownership: OwnershipSummary | None,
    ) -> tuple[RiskBand, str]:
        bands = [band]
        notes: list[str] = []
        hard_categories = (AdverseMediaCategory.SANCTIONS, AdverseMediaCategory.TERRORISM)
        # Severity distinguishes "the subject is designated / evading" (high, critical)
        # from sanctions-adjacent coverage such as a settled OFAC penalty (medium, low).
        # Real research surfaces the latter for many large legitimate companies, and a
        # rule that rates them all PROHIBITED cannot discriminate; the actual designation
        # signal is the deterministic watchlist screen, which escalates independently.
        if any(
            f.category in hard_categories and f.severity in (Severity.HIGH, Severity.CRITICAL)
            for f in adverse_media
        ):
            bands.append(RiskBand.PROHIBITED)
            notes.append(
                "high-severity sanctions/terrorism adverse-media hit forces a PROHIBITED band"
            )
        elif any(f.category in hard_categories for f in adverse_media):
            bands.append(RiskBand.HIGH)
            notes.append("sanctions-related adverse media raises the band to at least HIGH")
        elif adverse_media:
            bands.append(RiskBand.HIGH)
            notes.append("adverse-media findings raise the band to at least HIGH")
        if ownership and any(o.is_pep for o in ownership.owners):
            bands.append(RiskBand.HIGH)
            notes.append("PEP beneficial owner raises the band to at least HIGH")

        final = g.max_band(bands) or band
        if notes:
            suffix = " Risk policy: " + "; ".join(notes) + "."
            rationale = (rationale + suffix).strip()
        return final, rationale
