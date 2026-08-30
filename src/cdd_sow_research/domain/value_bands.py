"""Deterministic value-band arithmetic for SoW reconciliation.

The domain deliberately stores wealth as *bands* (``"USD 25m-50m"``), never spurious
precise figures. Reconciliation, however, needs arithmetic. This module is the
auditable bridge: it parses a band into a numeric ``(low, high)`` range, lets the gap
engine sum and compare ranges, and formats a range back into a band for display. It is
pure standard library and fully deterministic, so an auditor can recompute any number.

Scope of the reference parser: a single currency per case (the magnitudes are what the
reconciliation compares). Supported shapes (case-insensitive, whitespace-tolerant)::

    "USD 25m-50m"   "SGD 500k - 1m"   "USD 10m"   ">USD 50m"   "1m-5m"   ""

Unparseable or empty bands yield ``None`` (treated as "no figure"), never an exception.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_SUFFIX: dict[str, float] = {
    "k": 1_000.0,
    "m": 1_000_000.0,
    "mm": 1_000_000.0,
    "bn": 1_000_000_000.0,
    "b": 1_000_000_000.0,
}

# A single magnitude like "25m", "500k", "1.5bn", "750000".
_NUM = re.compile(r"(?P<num>\d+(?:\.\d+)?)\s*(?P<suffix>bn|mm|[kmb])?", re.IGNORECASE)
# A leading currency code (3 letters) if present, e.g. "USD", "SGD".
_CCY = re.compile(r"^\s*([A-Za-z]{3})\b")


@dataclass(frozen=True, slots=True)
class Range:
    """A numeric ``[low, high]`` money range in the case's base currency unit."""

    low: float
    high: float

    @property
    def midpoint(self) -> float:
        return (self.low + self.high) / 2.0


def currency_of(band: str) -> str:
    """Return the 3-letter currency code in ``band`` (upper-cased), or ``""``."""
    m = _CCY.match(band or "")
    return m.group(1).upper() if m else ""


def _magnitude(token: str) -> float | None:
    m = _NUM.fullmatch(token.strip())
    if not m:
        return None
    value = float(m.group("num"))
    suffix = (m.group("suffix") or "").lower()
    return value * _SUFFIX.get(suffix, 1.0)


def parse_band(band: str | None) -> Range | None:
    """Parse ``"USD 25m-50m"`` (and the shapes above) into a :class:`Range`.

    ``">USD 50m"`` becomes an open-ended range ``[50m, 75m]`` (a +50% headroom), so an
    "over X" declaration still contributes a defensible figure to the math. Returns
    ``None`` for empty/unparseable input rather than raising.
    """
    if not band:
        return None
    text = band.strip()
    open_ended = text.startswith(">") or text.lower().startswith("over ")
    # Strip any leading comparator first, then the currency prefix, so only magnitudes
    # remain (order matters for inputs like ">USD 50m").
    text = re.sub(r"^\s*(?:>|<|over|under|approx\.?|~|=)\s*", "", text, flags=re.IGNORECASE)
    text = _CCY.sub("", text)
    text = text.replace(",", "")

    parts = [p for p in re.split(r"\s*(?:-|–|to)\s*", text) if p.strip()]
    nums = [m for m in (_magnitude(p) for p in parts) if m is not None]
    if not nums:
        return None
    if len(nums) == 1:
        lo = nums[0]
        return Range(lo, lo * 1.5) if open_ended else Range(lo, lo)
    return Range(min(nums), max(nums))


def _fmt_magnitude(value: float) -> str:
    """Format a number back into a compact magnitude (``"25m"``, ``"500k"``)."""
    for suffix, scale in (("bn", 1e9), ("m", 1e6), ("k", 1e3)):
        if abs(value) >= scale:
            scaled = value / scale
            text = f"{scaled:.1f}".rstrip("0").rstrip(".")
            return f"{text}{suffix}"
    return f"{value:.0f}"


def format_range(rng: Range | None, currency: str = "USD") -> str:
    """Format a :class:`Range` back into a display band, e.g. ``"USD 25m-50m"``."""
    if rng is None:
        return ""
    ccy = (currency or "").upper().strip()
    prefix = f"{ccy} " if ccy else ""
    if abs(rng.low - rng.high) < 1.0:
        return f"{prefix}{_fmt_magnitude(rng.low)}"
    return f"{prefix}{_fmt_magnitude(rng.low)}-{_fmt_magnitude(rng.high)}"


#: The reference band ladder, in base currency units. A precise figure the model should
#: never have emitted is widened to the ladder band containing it, so one statement read
#: twice cannot produce "SGD 12.4m" once and "SGD 10m-15m" the next time: the F4
#: laptop/deployment pair diverged on exactly that shape on 2026-08-30, same evidence,
#: same model, different formatting. The rungs are order-of-magnitude thirds
#: (1 / 3 / 5 / 10 per decade), matching the granularity the narrative model already
#: uses when it does answer in bands. A fork tunes wealth policy, not this reference
#: arithmetic; the ladder is deliberately here with the parser it serves.
LADDER: tuple[float, ...] = (
    0.0,
    10_000.0,
    30_000.0,
    50_000.0,
    100_000.0,
    300_000.0,
    500_000.0,
    1_000_000.0,
    3_000_000.0,
    5_000_000.0,
    10_000_000.0,
    15_000_000.0,
    30_000_000.0,
    50_000_000.0,
    100_000_000.0,
    300_000_000.0,
    500_000_000.0,
    1_000_000_000.0,
)


def snap_to_band(band: str | None) -> str:
    """Enforce "a band, never a spurious precise figure" deterministically.

    A value that parses to a single point (``"SGD 12400000"``, ``"12.4m"``) is widened
    to the :data:`LADDER` band containing it and formatted with its own currency
    (``"SGD 10m-15m"``). A value that is already a range, and anything unparseable, is
    returned as written: this function removes false precision, never information.
    Values above the top rung become an open-ended ``">CCY 1bn"``, which
    :func:`parse_band` already reads back.
    """
    if not band:
        return ""
    rng = parse_band(band)
    if rng is None or abs(rng.high - rng.low) >= 1.0:
        return band.strip()
    value = rng.low
    ccy = currency_of(band)
    if value >= LADDER[-1]:
        prefix = f"{ccy} " if ccy else ""
        return f">{prefix}{_fmt_magnitude(LADDER[-1])}"
    for low, high in zip(LADDER, LADDER[1:], strict=False):
        if low <= value < high:
            return format_range(Range(low, high), ccy)
    return band.strip()  # pragma: no cover - the ladder starts at 0, so unreachable


def sum_bands(bands: list[str], currency: str = "USD") -> tuple[Range | None, str]:
    """Sum parseable bands into a total ``(Range, formatted_band)``.

    Unparseable/empty bands contribute nothing. If none parse, returns ``(None, "")``.
    """
    ranges = [r for r in (parse_band(b) for b in bands) if r is not None]
    if not ranges:
        return None, ""
    total = Range(sum(r.low for r in ranges), sum(r.high for r in ranges))
    return total, format_range(total, currency)


def coverage_pct(declared: Range | None, evidenced: Range | None) -> float:
    """Evidenced-over-declared coverage on midpoints, clamped to ``[0, 1]``.

    No declared figure -> 0.0 (cannot claim coverage of an unknown). No evidenced
    figure against a real declaration -> 0.0.
    """
    if declared is None or declared.midpoint <= 0:
        return 0.0
    if evidenced is None:
        return 0.0
    return max(0.0, min(1.0, evidenced.midpoint / declared.midpoint))
