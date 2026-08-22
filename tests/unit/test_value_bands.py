"""Unit tests for the deterministic value-band arithmetic."""

from __future__ import annotations

import pytest

from cdd_sow_research.domain import value_bands as vb


@pytest.mark.parametrize(
    "band,low,high",
    [
        ("USD 25m-50m", 25_000_000, 50_000_000),
        ("SGD 500k - 1m", 500_000, 1_000_000),
        ("USD 10m", 10_000_000, 10_000_000),
        ("1.5bn-2bn", 1_500_000_000, 2_000_000_000),
        ("USD 750,000", 750_000, 750_000),
    ],
)
def test_parse_band(band: str, low: float, high: float) -> None:
    rng = vb.parse_band(band)
    assert rng is not None
    assert rng.low == low
    assert rng.high == high


def test_parse_open_ended_adds_headroom() -> None:
    rng = vb.parse_band(">USD 50m")
    assert rng is not None
    assert rng.low == 50_000_000
    assert rng.high == 75_000_000  # +50% headroom


@pytest.mark.parametrize("bad", ["", None, "n/a", "lots", "band n/a"])
def test_parse_unparseable_is_none(bad) -> None:
    assert vb.parse_band(bad) is None


def test_currency_detection() -> None:
    assert vb.currency_of("USD 25m-50m") == "USD"
    assert vb.currency_of("sgd 1m") == "SGD"
    assert vb.currency_of("25m") == ""


def test_sum_bands_adds_ranges() -> None:
    total, band = vb.sum_bands(["USD 25m-50m", "USD 10m-25m", "n/a"])
    assert total is not None
    assert total.low == 35_000_000
    assert total.high == 75_000_000
    assert band == "USD 35m-75m"


def test_sum_bands_empty() -> None:
    assert vb.sum_bands(["", "n/a"]) == (None, "")


def test_coverage_pct_uses_midpoints_and_clamps() -> None:
    assert vb.coverage_pct(vb.parse_band("USD 100m"), vb.parse_band("USD 40m-60m")) == 0.5
    # Over-evidenced clamps to 1.0.
    assert vb.coverage_pct(vb.parse_band("USD 10m"), vb.parse_band("USD 50m")) == 1.0
    # No declared figure -> no claimable coverage.
    assert vb.coverage_pct(None, vb.parse_band("USD 10m")) == 0.0
    assert vb.coverage_pct(vb.parse_band("USD 10m"), None) == 0.0


def test_format_range_roundtrip() -> None:
    assert vb.format_range(vb.parse_band("USD 25m-50m"), "USD") == "USD 25m-50m"
    assert vb.format_range(vb.parse_band("USD 10m"), "USD") == "USD 10m"
    assert vb.format_range(None) == ""
