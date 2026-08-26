"""The number is the domain's; the prose is the model's.

``pairing.EXEMPT`` has asserted "The model narrates; it never produces the number" since it was
written, and until 2026-08-27 that sentence was false: ``RiskRatingService.rate`` read the band,
the score and the weighted factors straight out of the LLM response. That is why the paired
demonstration diverged on exactly those fields. The published portability claim says policy
never changes between profiles -- and a band produced by a model is not policy, it is model
output, so a laptop and a deployment running different models gave different answers to the most
consequential field in the dossier.

The test that matters is not "does it produce the right number" but "can the model change it".
So the stub below returns a deliberately absurd rating, and the assertion is that none of it
reaches the dossier.
"""

from __future__ import annotations

import json
from typing import Any

from cdd_sow_research.domain.models import (
    AdverseMediaFinding,
    RiskBand,
    SourceOfWealthNarrative,
    Subject,
    SubjectType,
)
from cdd_sow_research.domain.risk_service import RiskRatingService
from cdd_sow_research.domain.scorecard_service import RiskScorecardService


class _LyingLlm:
    """Answers every rating request with a maximal band and a score to match."""

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, request: Any) -> Any:
        self.calls += 1
        return type(
            "R",
            (),
            {
                "text": json.dumps(
                    {
                        "band": "prohibited",
                        "score": 1.0,
                        "factors": [
                            {"name": "invented_by_the_model", "weight": 1.0, "present": True}
                        ],
                        "rationale": "the narrative the reviewer reads",
                        "used_source_ids": [],
                    }
                ),
                "usage": None,
                "model": "stub",
            },
        )()


def _subject() -> Subject:
    return Subject(id="acme", name="Acme Holdings", type=SubjectType.ENTITY, jurisdiction="SG")


def _rate(llm: _LyingLlm, media: tuple[AdverseMediaFinding, ...] = ()):
    service = RiskRatingService(llm=llm, tracer=None, scorecard=RiskScorecardService())
    return service.rate(
        _subject(),
        SourceOfWealthNarrative(subject_id="acme", narrative="n", sources=(), confidence=0.9),
        media,
        None,
        [],
        "tester",
    )


def test_the_model_cannot_set_the_band_or_the_score() -> None:
    llm = _LyingLlm()
    expected = RiskScorecardService().score(_subject())

    rating = _rate(llm)

    assert llm.calls == 1, "the model is still asked, because it writes the rationale"
    assert rating.band == expected.band
    assert rating.score == expected.score
    assert rating.band is not RiskBand.PROHIBITED, "the model asked for PROHIBITED and was ignored"
    assert rating.score != 1.0


def test_the_model_cannot_invent_a_risk_factor() -> None:
    rating = _rate(_LyingLlm())

    names = {factor.name for factor in rating.factors}
    assert "invented_by_the_model" not in names
    assert names == {factor.name for factor in RiskScorecardService().score(_subject()).factors}


def test_the_model_still_writes_the_rationale() -> None:
    """Removing the call entirely would make the rating computed but no longer grounded."""
    rating = _rate(_LyingLlm())

    assert rating.rationale.startswith("the narrative the reviewer reads")


def test_the_same_inputs_always_produce_the_same_rating() -> None:
    """Replayability is the property the two profiles are being held to."""
    first, second = _rate(_LyingLlm()), _rate(_LyingLlm())

    assert (first.band, first.score) == (second.band, second.score)
    assert [(f.name, f.weight) for f in first.factors] == [
        (f.name, f.weight) for f in second.factors
    ]
