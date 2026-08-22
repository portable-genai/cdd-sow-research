"""Deterministic country / jurisdiction risk for the CDD risk scorecard.

A small, auditable table derived from the **FATF public lists** plus a coarse regional
baseline. Pure and offline; the same country always yields the same level, so the
scorecard is reproducible. The lists change over time — treat this as a refreshable
reference (a production build would sync FATF + an internal country-risk policy), but the
*shape* (call-for-action / increased-monitoring / baseline) is stable.
"""

from __future__ import annotations

from dataclasses import dataclass

from .policy import CountryRiskPolicy

# The default FATF-derived lists live in policy.CountryRiskPolicy; override them via the
# settings ``policy.country_risk`` section (or pass a custom policy) when the lists are
# refreshed or an internal country-risk policy differs.
_DEFAULT_POLICY = CountryRiskPolicy()


@dataclass(frozen=True, slots=True)
class CountryRisk:
    """A country's risk level and the reason, for the scorecard geography dimension."""

    code: str
    level: str  # "prohibited" | "high" | "medium" | "low"
    score: float  # 0..1 contribution to the geography dimension
    reason: str


def country_risk(code: str | None, policy: CountryRiskPolicy = _DEFAULT_POLICY) -> CountryRisk:
    """Return the deterministic risk level for an ISO-3166 alpha-2 country ``code``."""
    c = (code or "").strip().upper()
    if not c:
        return CountryRisk("", "medium", 0.5, "Country unknown — treated as medium risk.")
    if c in policy.call_for_action:
        return CountryRisk(c, "prohibited", 1.0, "FATF call-for-action jurisdiction.")
    if c in policy.increased_monitoring:
        return CountryRisk(c, "high", 0.8, "FATF increased-monitoring (grey list) jurisdiction.")
    if c in policy.elevated:
        return CountryRisk(c, "high", 0.75, "Elevated-risk jurisdiction (internal policy).")
    return CountryRisk(c, "low", 0.2, "No FATF listing; standard jurisdiction.")
