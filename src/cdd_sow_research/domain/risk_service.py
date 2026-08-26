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
    RiskScorecard,
    ScreeningResult,
    Severity,
    SourceOfWealthNarrative,
    Subject,
)
from .prompts import _CITATION_RULES, RISK_SYSTEM, RISK_USER
from .scorecard_service import RiskScorecardService


def _owner_is_pep(ownership: OwnershipSummary | None) -> bool:
    """Whether any resolved beneficial owner is a politically exposed person."""
    return any(owner.is_pep for owner in (ownership.owners if ownership else ()))


def _factors_from_scorecard(scorecard: RiskScorecard) -> tuple[RiskFactor, ...]:
    """Present the scorecard's weighted dimensions as the dossier's risk factors.

    ``present`` is whether the dimension actually contributed rather than whether it was
    considered: every dimension is always scored, so a factor list where all of them read
    "present" tells a reviewer nothing about which ones drove the band.
    """
    return tuple(
        RiskFactor(
            name=factor.name,
            weight=factor.weight,
            present=factor.score > 0.0,
            detail=factor.detail,
        )
        for factor in scorecard.factors
    )


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

    def __init__(
        self, llm: Any, tracer: Any, scorecard: RiskScorecardService | None = None
    ) -> None:
        self._llm = llm
        self._tracer = tracer
        # Defaulted rather than required so every existing construction keeps working; a
        # deployment that carries bank-owned policy passes RiskScorecardService.from_policy.
        self._scorecard = scorecard or RiskScorecardService()

    def rate(
        self,
        subject: Subject,
        sow: SourceOfWealthNarrative,
        adverse_media: tuple[AdverseMediaFinding, ...],
        ownership: OwnershipSummary | None,
        passages: list[Any],
        actor: str,
        screening: ScreeningResult | None = None,
    ) -> RiskRating:
        """Assign a risk rating for ``subject`` grounded in the case evidence.

        **The number is the domain's, and the prose is the model's.** The band, the score and
        the weighted factors come from :class:`RiskScorecardService`, which is pure and
        replayable: the same inputs always yield the same score and tier, so an auditor can
        recompute the decision and two profiles cannot disagree about it.

        They used to come from the LLM, and that is why they disagreed. The paired
        demonstration compares the risk figures across a laptop and a deployment, and the
        published claim is that policy never changes between profiles -- but a band produced by
        a model is not policy, it is model output, so two different models gave two different
        answers to the most consequential field in the dossier. ``pairing.EXEMPT`` has asserted
        "the model narrates; it never produces the number" since it was written. Until
        2026-08-27 that sentence was false.

        The LLM call is still made, and still matters: it writes the rationale a reviewer
        reads and names the passages it relied on, which is what makes the rating grounded
        rather than merely computed. What it can no longer do is decide the outcome.
        """
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
        rationale = str(parsed.get("rationale") or "").strip()
        citations = g.citations_for_source_ids(
            g.as_str_list(parsed.get("used_source_ids")), list(passages)
        )

        # The scorecard decides. Its inputs are the deterministic ones -- the subject's own
        # attributes, the watchlist screen, the adverse-media findings and whether an owner is
        # a PEP -- so both profiles compute the same figures from the same evidence.
        scorecard = self._scorecard.score(
            subject,
            screening=screening,
            adverse_media=adverse_media,
            is_pep=_owner_is_pep(ownership),
        )
        band = scorecard.band
        score = scorecard.score
        factors = _factors_from_scorecard(scorecard)

        # Hard signals still raise the band and never soften it. Kept after the scorecard
        # rather than folded into it: the scorecard weighs, this refuses, and a refusal that
        # can be outvoted by a weighting is not a refusal.
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
