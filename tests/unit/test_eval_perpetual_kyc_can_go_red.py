"""The ``pkyc_priority`` eval metric must be able to go RED (systemic finding 8).

An eval metric that re-reads the product's own output can never fail: it agrees with
whatever the engine did. ``pkyc_priority`` scores the engine against an INDEPENDENT
oracle, the golden set's own ``perpetual_kyc.expected_priority``, so a mis-tuned engine
disagrees with the dataset and the metric drops below its threshold.

This test proves exactly that, per change kind, using
``agent_eval_kit.assert_each_can_go_red``: the shipped engine scores green, and a
deliberately broken engine (one whose policy can never escalate anything) scores red.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from agent_eval_kit import assert_each_can_go_red

from cdd_sow_research.domain.perpetual_kyc import PerpetualKycEngine
from cdd_sow_research.domain.policy import PerpetualKycPolicy

_REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("run_eval", _REPO / "eval" / "run_eval.py")
run_eval = importlib.util.module_from_spec(_spec)
sys.modules["run_eval"] = run_eval
_spec.loader.exec_module(run_eval)  # type: ignore[union-attr]

_THRESHOLD = run_eval.THRESHOLDS["pkyc_priority"]

#: An engine that cannot escalate: nothing raises the score and every queue priority
#: collapses to the same floor, so it disagrees with any golden case expecting movement.
_BROKEN = PerpetualKycEngine.from_policy(
    PerpetualKycPolicy(
        severity_uplift={"low": 0.0, "medium": 0.0, "high": 0.0, "critical": 0.0},
        source_weight={"sanctions": 0.0, "adverse_media": 0.0, "registry": 0.0},
        max_uplift=0.0,
        urgent_score=99.0,
        high_score=99.0,
        low_score=0.0,
    )
)
_SHIPPED = PerpetualKycEngine.from_policy(PerpetualKycPolicy())


def _examples_by_change() -> dict[str, list]:
    """The golden set grouped by the change each case simulates (per-segment proof)."""
    examples = run_eval.load_golden(run_eval.DEFAULT_DATASET)
    grouped: dict[str, list] = {}
    for example in examples:
        grouped.setdefault(example.pkyc_change, []).append(example)
    return grouped


def test_the_dataset_exercises_more_than_one_change_kind():
    """A single-segment golden set would make the per-segment proof vacuous."""
    grouped = _examples_by_change()
    assert len(grouped) >= 3, f"expected several pKYC change kinds, got {sorted(grouped)}"
    expectations = {ex.expected_pkyc_priority for group in grouped.values() for ex in group}
    assert len(expectations) >= 3, "the golden priorities must not all be the same value"


def test_pkyc_priority_metric_can_go_red_per_change_kind():
    grouped = _examples_by_change()
    cases = {
        change: (_SHIPPED, _BROKEN)
        for change, examples in grouped.items()
        # "no_change" is the quiet segment: a broken engine agrees with it by accident,
        # so it cannot prove a red and is excluded from the falsely-green check.
        if change != "no_change"
    }
    assert cases, "no scoring segment available to prove the metric can fail"

    for change, engines in cases.items():
        examples = grouped[change]
        assert_each_can_go_red(
            lambda engine, examples=examples: run_eval.score_perpetual_kyc(engine, examples),
            {change: engines},
            threshold=_THRESHOLD,
            metric="pkyc_priority",
        )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
