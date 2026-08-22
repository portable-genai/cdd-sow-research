"""The ``ubo_accuracy`` eval metric must be able to go RED (systemic finding 8).

An eval metric that re-reads the product's own output can never fail: it agrees with
whatever the engine did. ``ubo_accuracy`` scores the engine against an INDEPENDENT oracle,
the golden set's own ``ubo.expected_owners`` / ``expected_control_basis`` /
``expected_flags``, so an engine that resolves the structure differently disagrees with the
dataset and the metric drops below its threshold.

This test proves exactly that, per chain type, using
``agent_eval_kit.assert_each_can_go_red``: the shipped engine scores green, and a
deliberately broken engine scores red.

The broken engine is not a strawman. ``max_depth=1`` is precisely the flat, one-hop reader
this whole module exists to replace: it sees the immediate shareholders and stops, so it
reports the holding company instead of the person two layers above it. If the metric could
not tell that engine apart from the shipped one, the metric would be worthless.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from agent_eval_kit import assert_each_can_go_red

from cdd_sow_research.domain.policy import CountryRiskPolicy, UboGraphPolicy
from cdd_sow_research.domain.ubo_graph import UboGraphEngine

_REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("run_eval", _REPO / "eval" / "run_eval.py")
run_eval = importlib.util.module_from_spec(_spec)
sys.modules["run_eval"] = run_eval
_spec.loader.exec_module(run_eval)  # type: ignore[union-attr]

_THRESHOLD = run_eval.THRESHOLDS["ubo_accuracy"]

#: The flat one-hop reader: it walks a single layer and stops, which is the exact failure
#: mode a cross-jurisdiction UBO graph exists to fix.
_BROKEN = UboGraphEngine.from_policy(UboGraphPolicy(max_depth=1), CountryRiskPolicy())
_SHIPPED = UboGraphEngine.from_policy(UboGraphPolicy(), CountryRiskPolicy())

#: The senior-managing-official rung is a FALLBACK, reached only when no earlier rung holds.
#: A one-hop reader cannot red it (the directors are direct edges, so the truncated walk
#: reaches the same verdict). This engine breaks the fallback the way a real regression
#: would: a zero board-majority ratio lets the BOARD rung fire on any single seat, so
#: control resolves one rung early and the fallback is never reached, which is a
#: disagreement the metric must catch.
_FALLBACK_CHAIN = "senior_managing_official_fallback"
_FALLBACK_BROKEN = UboGraphEngine.from_policy(
    UboGraphPolicy(board_majority_ratio=0.0), CountryRiskPolicy()
)


def _examples_by_chain() -> dict[str, list]:
    """The golden set grouped by the structure each case declares (per-segment proof)."""
    grouped: dict[str, list] = {}
    for example in run_eval.load_golden(run_eval.DEFAULT_DATASET):
        if not example.ubo:
            continue
        grouped.setdefault(example.ubo_chain, []).append(example)
    return grouped


def test_the_dataset_exercises_more_than_one_chain_type():
    """A single-shape golden set would make the per-segment proof vacuous."""
    grouped = _examples_by_chain()
    assert len(grouped) >= 4, f"expected several declared chain types, got {sorted(grouped)}"
    assert {
        "layered_chain",
        "cross_holding_cycle",
        "shell_pass_through",
        _FALLBACK_CHAIN,
    } <= set(grouped)
    bases_present = {run_eval.expected_ubo_outcome(ex)[1] for g in grouped.values() for ex in g}
    assert "senior_managing_official" in bases_present, "the SMO fallback rung must be exercised"
    bases = {run_eval.expected_ubo_outcome(ex)[1] for group in grouped.values() for ex in group}
    assert len(bases) >= 2, "the golden control bases must not all be the same value"


def test_every_declared_chain_is_actually_reachable():
    """A case whose declared layers never resolve would score green vacuously."""
    for chain, examples in _examples_by_chain().items():
        for example in examples:
            owners, basis, flags = run_eval.computed_ubo_outcome(_SHIPPED, example)
            assert (owners, basis, flags) == run_eval.expected_ubo_outcome(example), (
                f"{example.id} ({chain}) does not match its own declaration"
            )


def test_ubo_accuracy_metric_can_go_red_per_chain_type():
    grouped = _examples_by_chain()
    cases = {
        chain: (_SHIPPED, _BROKEN)
        for chain in grouped
        # "no_structure" is the quiet segment: there is nothing to walk, so a one-hop
        # reader agrees with it by accident and it cannot prove a red. The SMO fallback is
        # excluded here for the same reason (its directors are direct edges the truncated
        # walk still sees) and gets its own fallback-breaking proof below.
        if chain not in ("no_structure", _FALLBACK_CHAIN)
    }
    assert cases, "no scoring segment available to prove the metric can fail"

    for chain, engines in cases.items():
        examples = grouped[chain]
        assert_each_can_go_red(
            lambda engine, examples=examples: run_eval.score_ubo_graph(engine, examples),
            {chain: engines},
            threshold=_THRESHOLD,
            metric="ubo_accuracy",
        )


def test_the_metric_goes_red_when_the_senior_managing_official_fallback_is_broken():
    """The SMO fallback rung has its own falsification proof.

    A one-hop reader cannot red this segment, so it is excluded from the generic proof
    above. Here the deliberately broken engine resolves control one rung EARLY (the board
    rung fires on any single seat), never reaching the fallback, so the shipped and broken
    engines disagree with the golden declaration differently and the metric drops.
    """
    examples = _examples_by_chain().get(_FALLBACK_CHAIN)
    assert examples, "the golden set must declare a senior-managing-official fallback case"
    for example in examples:
        assert run_eval.expected_ubo_outcome(example)[1] == "senior_managing_official"

    assert_each_can_go_red(
        lambda engine, examples=examples: run_eval.score_ubo_graph(engine, examples),
        {_FALLBACK_CHAIN: (_SHIPPED, _FALLBACK_BROKEN)},
        threshold=_THRESHOLD,
        metric="ubo_accuracy",
    )


def test_the_metric_also_falls_when_only_the_arithmetic_is_wrong():
    """A subtler red: the walk is right but the threshold is not, so the owners differ."""
    scorable = [ex for group in _examples_by_chain().values() for ex in group]
    mis_tuned = UboGraphEngine.from_policy(
        UboGraphPolicy(ownership_threshold_pct=99.0), CountryRiskPolicy()
    )

    assert run_eval.score_ubo_graph(_SHIPPED, scorable) >= _THRESHOLD
    assert run_eval.score_ubo_graph(mis_tuned, scorable) < _THRESHOLD


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
