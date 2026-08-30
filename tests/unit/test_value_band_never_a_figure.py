"""The wire contract "a band, never a spurious precise figure", enforced by the domain.

The F4 laptop/deployment pair diverged on 2026-08-30 with the same evidence and the same
model: one run answered ``SGD 10000000-15000000``, the other ``SGD 12400000``. Both are
the same claim about the money; only the second violates the contract the model was
given. The model does not always honour its prompt, so the domain does it for it.
"""

from __future__ import annotations

import pytest

from cdd_sow_research.domain import value_bands as vb


@pytest.mark.parametrize(
    ("figure", "band"),
    [
        ("SGD 12400000", "SGD 10m-15m"),
        ("SGD 3150000", "SGD 3m-5m"),
        ("USD 12.4m", "USD 10m-15m"),
        ("750000", "500k-1m"),
        ("SGD 0", "SGD 0-10k"),
    ],
)
def test_a_precise_figure_is_widened_to_its_ladder_band(figure: str, band: str) -> None:
    assert vb.snap_to_band(figure) == band


def test_a_real_range_is_kept_as_written() -> None:
    """Snapping removes false precision, never information."""

    assert vb.snap_to_band("SGD 10000000-15000000") == "SGD 10000000-15000000"
    assert vb.snap_to_band("USD 1m-5m") == "USD 1m-5m"


def test_unparseable_text_passes_through() -> None:
    assert vb.snap_to_band("substantial family wealth") == "substantial family wealth"
    assert vb.snap_to_band("") == ""
    assert vb.snap_to_band(None) == ""


def test_above_the_top_rung_becomes_open_ended_and_still_parses() -> None:
    snapped = vb.snap_to_band("SGD 2.5bn")
    assert snapped == ">SGD 1bn"
    assert vb.parse_band(snapped) is not None


def test_the_snapped_band_agrees_with_the_figure_it_came_from() -> None:
    """The widened band must still CONTAIN the original value: auditable, not invented."""

    for raw in ("SGD 12400000", "USD 42k", "7.7m"):
        original = vb.parse_band(raw)
        snapped = vb.parse_band(vb.snap_to_band(raw))
        assert original is not None and snapped is not None
        assert snapped.low <= original.low <= snapped.high
