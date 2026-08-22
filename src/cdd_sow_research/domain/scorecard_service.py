"""RiskScorecardService — a deterministic, risk-based CDD scorecard + tiering.

Computes an explicit, auditable customer-risk score across the standard dimensions
(customer type, geography/country, product, channel) plus financial-crime signals
(PEP, sanctions/watchlist hits, adverse media), then maps it to a :class:`RiskBand` and a
:class:`CddTier` (SDD / CDD / EDD). Pure and replayable — the same inputs always yield the
same score and tier, so an auditor can recompute the due-diligence decision. This
complements the LLM ``RiskRatingService`` with a documented scorecard.

Hard signals (open sanctions hit, PEP, FATF call-for-action country) force EDD and raise
the band; the score can never soften them. The output is decision-support — a checker
still disposes (P-06).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from . import country_risk as cr
from .models import (
    AdverseMediaCategory,
    AdverseMediaFinding,
    CddTier,
    RiskBand,
    RiskScorecard,
    ScorecardFactor,
    ScreeningResult,
    Subject,
    SubjectType,
)
from .policy import (
    _DEFAULT_CHANNEL_RISK,
    _DEFAULT_PRODUCT_RISK,
    _DEFAULT_WEIGHTS,
    CountryRiskPolicy,
    ScorecardPolicy,
)


@dataclass(frozen=True, slots=True)
class RiskScorecardService:
    """Pure, deterministic risk-based scorecard + CDD tiering.

    Every weight, table and threshold is a constructor field (bank-owned policy, see
    ``policy.ScorecardPolicy``); the defaults are the reference build's values.
    """

    weights: Mapping[str, float] = field(default_factory=lambda: dict(_DEFAULT_WEIGHTS))
    #: Coarse product/channel risk scores (0..1); callers pass a key, else "default".
    product_risk: Mapping[str, float] = field(default_factory=lambda: dict(_DEFAULT_PRODUCT_RISK))
    channel_risk: Mapping[str, float] = field(default_factory=lambda: dict(_DEFAULT_CHANNEL_RISK))
    #: Total score at/above which the band is HIGH (and tier EDD).
    edd_score: float = 0.6
    #: Total score below which the band is LOW (and tier may be SDD).
    sdd_score: float = 0.25
    #: FATF-derived country lists for the geography dimension.
    country_policy: CountryRiskPolicy = field(default_factory=CountryRiskPolicy)

    @classmethod
    def from_policy(
        cls, policy: ScorecardPolicy, country: CountryRiskPolicy | None = None
    ) -> RiskScorecardService:
        """Build the scorecard from bank-owned policy objects (settings-driven)."""
        return cls(
            weights=dict(policy.weights),
            product_risk=dict(policy.product_risk),
            channel_risk=dict(policy.channel_risk),
            edd_score=policy.edd_score,
            sdd_score=policy.sdd_score,
            country_policy=country or CountryRiskPolicy(),
        )

    def score(
        self,
        subject: Subject,
        *,
        screening: ScreeningResult | None = None,
        adverse_media: tuple[AdverseMediaFinding, ...] = (),
        is_pep: bool = False,
        product: str = "default",
        channel: str = "default",
    ) -> RiskScorecard:
        """Compute the scorecard for ``subject`` from its risk dimensions + signals."""
        geo = cr.country_risk(subject.jurisdiction, self.country_policy)
        pep = is_pep or self._screening_has(screening, pep=True)
        sanctioned = self._screening_has(screening, pep=False)
        media_score, media_detail = self._media(adverse_media)

        factors = (
            ScorecardFactor(
                "customer_type",
                self.weights.get("customer_type", 0.15),
                0.6 if subject.type is SubjectType.ENTITY else 0.4,
                f"{subject.type.value} customer.",
            ),
            ScorecardFactor(
                "geography", self.weights.get("geography", 0.30), geo.score, geo.reason
            ),
            ScorecardFactor(
                "product",
                self.weights.get("product", 0.15),
                self.product_risk.get(product, self.product_risk.get("default", 0.4)),
                f"product={product}",
            ),
            ScorecardFactor(
                "channel",
                self.weights.get("channel", 0.10),
                self.channel_risk.get(channel, self.channel_risk.get("default", 0.4)),
                f"channel={channel}",
            ),
            ScorecardFactor(
                "pep_exposure",
                self.weights.get("pep_exposure", 0.15),
                1.0 if pep else 0.0,
                "PEP exposure present." if pep else "No PEP exposure.",
            ),
            ScorecardFactor(
                "adverse_media", self.weights.get("adverse_media", 0.15), media_score, media_detail
            ),
        )
        total_w = sum(f.weight for f in factors) or 1.0
        total = round(sum(f.contribution for f in factors) / total_w, 4)

        hard: list[str] = []
        if sanctioned:
            hard.append("open sanctions/watchlist hit")
        if pep:
            hard.append("politically-exposed person")
        if geo.level == "prohibited":
            hard.append("FATF call-for-action jurisdiction")
        if any(
            f.category in (AdverseMediaCategory.SANCTIONS, AdverseMediaCategory.TERRORISM)
            for f in adverse_media
        ):
            hard.append("sanctions/terrorism adverse media")

        band, tier = self._band_tier(total, hard, geo.level)
        rationale = (
            f"Weighted risk {round(total * 100)}% across {len(factors)} dimensions"
            f" → {band.value.upper()} / {tier.value.upper()}."
            + (f" Hard signals: {', '.join(hard)}." if hard else "")
        )
        return RiskScorecard(
            factors=factors,
            score=total,
            band=band,
            tier=tier,
            rationale=rationale,
            hard_signals=tuple(hard),
        )

    # ------------------------------------------------------------------ #
    def _band_tier(self, total: float, hard: list[str], geo_level: str) -> tuple[RiskBand, CddTier]:
        if geo_level == "prohibited" or "open sanctions/watchlist hit" in hard:
            return RiskBand.PROHIBITED, CddTier.EDD
        if hard or total >= self.edd_score:
            return RiskBand.HIGH, CddTier.EDD
        if total < self.sdd_score:
            return RiskBand.LOW, CddTier.SDD
        return RiskBand.MEDIUM, CddTier.CDD

    @staticmethod
    def _screening_has(screening: ScreeningResult | None, *, pep: bool) -> bool:
        if screening is None:
            return False
        from .models import ListSource

        for a in screening.open_alerts:
            is_pep_src = a.match.entry.source is ListSource.PEP
            if is_pep_src == pep:
                return True
        return False

    @staticmethod
    def _media(adverse_media: tuple[AdverseMediaFinding, ...]) -> tuple[float, str]:
        if not adverse_media:
            return 0.0, "No adverse media."
        from .models import Severity

        order = {
            Severity.LOW: 0.3,
            Severity.MEDIUM: 0.5,
            Severity.HIGH: 0.8,
            Severity.CRITICAL: 1.0,
        }
        worst = max(order.get(f.severity, 0.5) for f in adverse_media)
        return worst, f"{len(adverse_media)} adverse-media finding(s)."
