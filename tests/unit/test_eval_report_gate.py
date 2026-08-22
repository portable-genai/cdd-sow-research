"""The promotion gate's aggregate verdict must fail closed on an empty evaluation.

:attr:`EvalReport.passed` was ``all(r.passed for r in self.results)``. ``all(())`` is
vacuously True, so a report that scored nothing reported PASSED, and ``eval/run_eval.py``
exits ``0`` on ``passed``: an evaluation that measured nothing certified a promotion.

The first three tests were RED against that form before the fix landed, which is the only
reason they are worth keeping. The last two pin the behaviour that must NOT change: a real
evaluation still passes, and one failing metric still fails.
"""

from __future__ import annotations

from cdd_sow_research.adapters.gcp.genai_eval import _examples_submitted
from cdd_sow_research.domain.kernel import EvalMetricResult, EvalReport

_DATASET = "golden.jsonl"


def _row(metric: str = "groundedness", *, passed: bool = True) -> EvalMetricResult:
    return EvalMetricResult(
        metric=metric,
        score=0.95 if passed else 0.10,
        threshold=0.80,
        passed=passed,
    )


def test_a_report_that_scored_NOTHING_does_not_pass_the_gate() -> None:
    """The exact fail-open: no metrics, no examples, and the old form said PASSED."""
    assert EvalReport(dataset=_DATASET, results=(), n_examples=0).passed is False


def test_examples_scored_but_no_metric_computed_does_not_pass() -> None:
    """A run that loaded data and produced no metric proves nothing about quality."""
    assert EvalReport(dataset=_DATASET, results=(), n_examples=12).passed is False


def test_metric_rows_over_zero_examples_do_not_pass() -> None:
    """Rows synthesised over an empty dataset are not evidence."""
    assert EvalReport(dataset=_DATASET, results=(_row(),), n_examples=0).passed is False


def test_a_real_passing_evaluation_still_passes() -> None:
    assert EvalReport(dataset=_DATASET, results=(_row(),), n_examples=12).passed is True


def test_one_failing_metric_still_fails() -> None:
    report = EvalReport(
        dataset=_DATASET,
        results=(_row(), _row("safety", passed=False)),
        n_examples=12,
    )
    assert report.passed is False


# --------------------------------------------------------------------------- #
# The evidence the verdict now rests on: how many examples were actually scored
# --------------------------------------------------------------------------- #


def test_the_submitted_example_count_is_the_rows_of_the_dataset(tmp_path) -> None:
    """The cloud adapter reports what it submitted, matching the offline gate's count."""
    dataset = tmp_path / "golden.jsonl"
    dataset.write_text('{"a": 1}\n{"a": 2}\n{"a": 3}\n', encoding="utf-8")
    assert _examples_submitted(str(dataset)) == 3


def test_blank_lines_are_not_counted_as_examples(tmp_path) -> None:
    dataset = tmp_path / "golden.jsonl"
    dataset.write_text('{"a": 1}\n\n  \n{"a": 2}\n', encoding="utf-8")
    assert _examples_submitted(str(dataset)) == 2


def test_an_unreadable_dataset_counts_zero_and_therefore_FAILS_the_gate(tmp_path) -> None:
    """Fail closed: a dataset that could not be read asserts no quality at all."""
    missing = str(tmp_path / "absent.jsonl")
    assert _examples_submitted(missing) == 0
    assert EvalReport(dataset=missing, results=(_row(),), n_examples=0).passed is False
