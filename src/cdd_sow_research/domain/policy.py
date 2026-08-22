"""Bank-owned risk policy as data (the numbers a compliance function tunes).

Every threshold, weight, cadence and list a bank's financial-crime policy owner would
want to set is collected here as frozen dataclasses with defaults equal to the
reference build's historical constants. The deterministic engines take these objects
as constructor parameters, so an institution changes policy through configuration
(``config/settings.yaml`` ``policy:`` section, parsed by ``config.py``), never by
editing engine code.

Pure standard library (this is domain code); parsing from a plain mapping lives here
so the wiring layer stays thin. ``from_mapping`` accepts the dict shape produced by
YAML/JSON and tolerates env-interpolated strings for numeric fields.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


def _as_float(value: Any, default: float) -> float:
    if value is None or value == "":
        return default
    return float(value)


def _as_int(value: Any, default: int) -> int:
    if value is None or value == "":
        return default
    return int(value)


def _str_set(values: Any) -> frozenset[str]:
    return frozenset(str(v) for v in (values or ()))


# --------------------------------------------------------------------------- #
# Gap analysis (Source of Wealth reconciliation)
# --------------------------------------------------------------------------- #
_DEFAULT_MANDATORY_DOCS: dict[str, frozenset[str]] = {
    "asset_sale": frozenset({"bank_statement"}),
    "business_ownership": frozenset({"registry_extract"}),
    "investments": frozenset({"fin_statement"}),
    "employment": frozenset({"fin_statement"}),
}

_DEFAULT_STALE_DOCS: frozenset[str] = frozenset({"bank_statement", "fin_statement"})


@dataclass(frozen=True)
class GapPolicy:
    """Tolerances + mandatory-document tables for the SoW gap engine."""

    #: A total coverage below ``1 - delta_tolerance`` raises an UNRECONCILED_DELTA gap.
    delta_tolerance: float = 0.15
    #: Documents in ``stale_doc_types`` older than this many days are STALE_EVIDENCE.
    stale_days: int = 180
    #: Per wealth-source kind, the doc type(s) policy requires before "fully evidenced".
    mandatory_docs: Mapping[str, frozenset[str]] = field(
        default_factory=lambda: dict(_DEFAULT_MANDATORY_DOCS)
    )
    #: Doc types whose evidential value decays with age (staleness window applies).
    stale_doc_types: frozenset[str] = _DEFAULT_STALE_DOCS

    @staticmethod
    def from_mapping(raw: Mapping[str, Any]) -> GapPolicy:
        defaults = GapPolicy()
        mandatory = raw.get("mandatory_docs")
        return GapPolicy(
            delta_tolerance=_as_float(raw.get("delta_tolerance"), defaults.delta_tolerance),
            stale_days=_as_int(raw.get("stale_days"), defaults.stale_days),
            mandatory_docs=(
                {str(k): _str_set(v) for k, v in mandatory.items()}
                if isinstance(mandatory, Mapping)
                else dict(_DEFAULT_MANDATORY_DOCS)
            ),
            stale_doc_types=(
                _str_set(raw["stale_doc_types"])
                if "stale_doc_types" in raw
                else defaults.stale_doc_types
            ),
        )


# --------------------------------------------------------------------------- #
# Source of Funds
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SofPolicy:
    """Tolerances for the Source-of-Funds reconciliation engine."""

    #: Coverage below ``1 - delta_tolerance`` raises an unevidenced-funding gap.
    delta_tolerance: float = 0.15
    #: Evidenced inflow above expected by more than this fraction = activity mismatch.
    activity_tolerance: float = 0.25

    @staticmethod
    def from_mapping(raw: Mapping[str, Any]) -> SofPolicy:
        defaults = SofPolicy()
        return SofPolicy(
            delta_tolerance=_as_float(raw.get("delta_tolerance"), defaults.delta_tolerance),
            activity_tolerance=_as_float(
                raw.get("activity_tolerance"), defaults.activity_tolerance
            ),
        )


# --------------------------------------------------------------------------- #
# Risk scorecard + CDD tiering
# --------------------------------------------------------------------------- #
_DEFAULT_WEIGHTS: dict[str, float] = {
    "customer_type": 0.15,
    "geography": 0.30,
    "product": 0.15,
    "channel": 0.10,
    "pep_exposure": 0.15,
    "adverse_media": 0.15,
}

_DEFAULT_PRODUCT_RISK: dict[str, float] = {
    "default": 0.4,
    "deposit": 0.3,
    "private_banking": 0.7,
    "trade_finance": 0.7,
    "correspondent": 0.9,
    "crypto": 0.9,
}

_DEFAULT_CHANNEL_RISK: dict[str, float] = {
    "default": 0.4,
    "branch": 0.2,
    "remote": 0.6,
    "introduced": 0.7,
    "non_face_to_face": 0.7,
}


@dataclass(frozen=True)
class ScorecardPolicy:
    """Weights, product/channel risk tables and tier thresholds for the scorecard."""

    weights: Mapping[str, float] = field(default_factory=lambda: dict(_DEFAULT_WEIGHTS))
    product_risk: Mapping[str, float] = field(default_factory=lambda: dict(_DEFAULT_PRODUCT_RISK))
    channel_risk: Mapping[str, float] = field(default_factory=lambda: dict(_DEFAULT_CHANNEL_RISK))
    #: Total score at/above which the band is HIGH (and tier EDD).
    edd_score: float = 0.6
    #: Total score below which the band is LOW (and tier may be SDD).
    sdd_score: float = 0.25

    @staticmethod
    def from_mapping(raw: Mapping[str, Any]) -> ScorecardPolicy:
        defaults = ScorecardPolicy()

        def _table(key: str, fallback: Mapping[str, float]) -> Mapping[str, float]:
            table = raw.get(key)
            if isinstance(table, Mapping):
                return {str(k): float(v) for k, v in table.items()}
            return dict(fallback)

        return ScorecardPolicy(
            weights=_table("weights", defaults.weights),
            product_risk=_table("product_risk", defaults.product_risk),
            channel_risk=_table("channel_risk", defaults.channel_risk),
            edd_score=_as_float(raw.get("edd_score"), defaults.edd_score),
            sdd_score=_as_float(raw.get("sdd_score"), defaults.sdd_score),
        )


# --------------------------------------------------------------------------- #
# Country / jurisdiction risk (FATF-derived, refreshable reference)
# --------------------------------------------------------------------------- #
_DEFAULT_CALL_FOR_ACTION: frozenset[str] = frozenset({"IR", "KP", "MM"})
_DEFAULT_INCREASED_MONITORING: frozenset[str] = frozenset(
    {"SY", "YE", "VE", "HT", "ML", "SS", "CD", "MZ", "BF", "NG", "ZA", "VN"}
)
_DEFAULT_ELEVATED: frozenset[str] = frozenset({"RU", "BY", "AF", "LY", "SO"})


@dataclass(frozen=True)
class CountryRiskPolicy:
    """The FATF-derived country lists the geography dimension scores against."""

    call_for_action: frozenset[str] = _DEFAULT_CALL_FOR_ACTION
    increased_monitoring: frozenset[str] = _DEFAULT_INCREASED_MONITORING
    elevated: frozenset[str] = _DEFAULT_ELEVATED

    @staticmethod
    def from_mapping(raw: Mapping[str, Any]) -> CountryRiskPolicy:
        defaults = CountryRiskPolicy()
        return CountryRiskPolicy(
            call_for_action=(
                _str_set(raw["call_for_action"])
                if "call_for_action" in raw
                else defaults.call_for_action
            ),
            increased_monitoring=(
                _str_set(raw["increased_monitoring"])
                if "increased_monitoring" in raw
                else defaults.increased_monitoring
            ),
            elevated=_str_set(raw["elevated"]) if "elevated" in raw else defaults.elevated,
        )


# --------------------------------------------------------------------------- #
# Ongoing monitoring / periodic review
# --------------------------------------------------------------------------- #
_DEFAULT_CADENCE_MONTHS: dict[str, int] = {
    "sdd": 60,  # simplified — every five years
    "cdd": 36,  # standard — every three years
    "edd": 12,  # enhanced — annually
}


@dataclass(frozen=True)
class MonitoringPolicy:
    """Risk-based review cadence + due-soon window for ongoing monitoring."""

    #: Cadence (months) per CDD tier value ("sdd" | "cdd" | "edd").
    cadence_months: Mapping[str, int] = field(default_factory=lambda: dict(_DEFAULT_CADENCE_MONTHS))
    #: A periodic review within this many days counts as "due soon".
    due_soon_days: int = 90

    @staticmethod
    def from_mapping(raw: Mapping[str, Any]) -> MonitoringPolicy:
        defaults = MonitoringPolicy()
        cadence = raw.get("cadence_months")
        return MonitoringPolicy(
            cadence_months=(
                {str(k): int(v) for k, v in cadence.items()}
                if isinstance(cadence, Mapping)
                else dict(_DEFAULT_CADENCE_MONTHS)
            ),
            due_soon_days=_as_int(raw.get("due_soon_days"), defaults.due_soon_days),
        )


# --------------------------------------------------------------------------- #
# Maker-checker escalation (P-06)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class EscalationPolicy:
    """Which risk bands / adverse-media categories escalate to enhanced review."""

    #: RiskBand values that escalate a case to enhanced review.
    escalating_bands: frozenset[str] = frozenset({"high", "prohibited"})
    #: AdverseMediaCategory values that escalate regardless of the risk band.
    escalating_media: frozenset[str] = frozenset({"sanctions", "terrorism"})

    @staticmethod
    def from_mapping(raw: Mapping[str, Any]) -> EscalationPolicy:
        defaults = EscalationPolicy()
        return EscalationPolicy(
            escalating_bands=(
                _str_set(raw["escalating_bands"])
                if "escalating_bands" in raw
                else defaults.escalating_bands
            ),
            escalating_media=(
                _str_set(raw["escalating_media"])
                if "escalating_media" in raw
                else defaults.escalating_media
            ),
        )


# --------------------------------------------------------------------------- #
# Perpetual KYC (continuous, signal-driven re-assessment)
# --------------------------------------------------------------------------- #
#: Score uplift a single NEW signal contributes, by its severity. These are the
#: reference build's values; a bank tunes them without touching the engine.
_DEFAULT_SEVERITY_UPLIFT: dict[str, float] = {
    "low": 0.02,
    "medium": 0.05,
    "high": 0.12,
    "critical": 0.20,
}

#: How much a signal family weighs relative to its severity uplift. A sanctions match is
#: taken at face value; media and registry movement are corroborating, not conclusive.
_DEFAULT_SOURCE_WEIGHT: dict[str, float] = {
    "sanctions": 1.0,
    "adverse_media": 0.8,
    "registry": 0.6,
}

#: Working days allowed to disposition a queued review, by priority.
_DEFAULT_SLA_DAYS: dict[str, int] = {
    "urgent": 1,
    "high": 5,
    "standard": 15,
    "low": 30,
}


@dataclass(frozen=True)
class PerpetualKycPolicy:
    """Bank-owned numbers behind the deterministic perpetual-KYC re-score and queue.

    The engine (``domain/perpetual_kyc.py``) reads every threshold from here, so the
    re-score is auditable policy arithmetic rather than a constant buried in code (B4).
    """

    #: Uplift per NEW signal, keyed by ``Severity`` value.
    severity_uplift: Mapping[str, float] = field(
        default_factory=lambda: dict(_DEFAULT_SEVERITY_UPLIFT)
    )
    #: Multiplier per ``SignalSource`` value applied to the severity uplift.
    source_weight: Mapping[str, float] = field(default_factory=lambda: dict(_DEFAULT_SOURCE_WEIGHT))
    #: Ceiling on the total uplift one run may add (a re-score never runs away).
    max_uplift: float = 0.50
    #: Fraction of a signal's uplift returned when it is no longer observed. Below 1.0
    #: on purpose: clearing a signal relieves risk more slowly than raising it.
    cleared_relief: float = 0.50
    #: Re-scored total at/above which the queue item is URGENT.
    urgent_score: float = 0.75
    #: Re-scored total at/above which the queue item is HIGH.
    high_score: float = 0.50
    #: Re-scored total below which the queue item is LOW.
    low_score: float = 0.25
    #: Days allowed to disposition a queued review, keyed by ``QueuePriority`` value.
    sla_days: Mapping[str, int] = field(default_factory=lambda: dict(_DEFAULT_SLA_DAYS))

    @staticmethod
    def from_mapping(raw: Mapping[str, Any]) -> PerpetualKycPolicy:
        defaults = PerpetualKycPolicy()
        uplift = raw.get("severity_uplift")
        weight = raw.get("source_weight")
        sla = raw.get("sla_days")
        return PerpetualKycPolicy(
            severity_uplift=(
                {str(k): float(v) for k, v in uplift.items()}
                if isinstance(uplift, Mapping)
                else dict(defaults.severity_uplift)
            ),
            source_weight=(
                {str(k): float(v) for k, v in weight.items()}
                if isinstance(weight, Mapping)
                else dict(defaults.source_weight)
            ),
            max_uplift=_as_float(raw.get("max_uplift"), defaults.max_uplift),
            cleared_relief=_as_float(raw.get("cleared_relief"), defaults.cleared_relief),
            urgent_score=_as_float(raw.get("urgent_score"), defaults.urgent_score),
            high_score=_as_float(raw.get("high_score"), defaults.high_score),
            low_score=_as_float(raw.get("low_score"), defaults.low_score),
            sla_days=(
                {str(k): int(v) for k, v in sla.items()}
                if isinstance(sla, Mapping)
                else dict(defaults.sla_days)
            ),
        )


# --------------------------------------------------------------------------- #
# Cross-jurisdiction UBO graph resolution
# --------------------------------------------------------------------------- #
#: Name tokens that declare a nominee / fiduciary role. Single words, matched against the
#: normalised name tokens, so a fork adds its market's vocabulary without an engine edit.
_DEFAULT_NOMINEE_TOKENS: tuple[str, ...] = (
    "nominee",
    "nominees",
    "trustee",
    "trustees",
    "fiduciary",
    "fiduciaries",
)

#: Registry filing statuses that read as dormant / struck off (substring match).
_DEFAULT_DORMANT_STATUSES: tuple[str, ...] = (
    "dormant",
    "struck",
    "inactive",
    "suspended",
    "liquidation",
)

#: How much each indicator contributes to the opacity score, which is clamped to [0, 1].
#: Deliberately additive and small: opacity is a REASON TO LOOK, and one indicator alone
#: must never present as a conclusion.
_DEFAULT_FLAG_WEIGHT: dict[str, float] = {
    "nominee_indicator": 0.25,
    "shell_indicator": 0.20,
    "circular_holding": 0.20,
    "depth_truncated": 0.15,
    "secrecy_jurisdiction": 0.15,
    "unresolved_layer": 0.20,
    "no_owner_at_threshold": 0.10,
}

#: Review severity a resolution earns from its opacity score, as descending ``(floor,
#: severity)`` bands: the first band the score clears is the one it earns, and a score below
#: every floor is LOW. A resolution carries no risk band and no queue priority to borrow, so
#: its Hrz7 severity is banded on how OPAQUE the structure is; these cut-offs are the bank's,
#: not a constant in the engine or the review adapter (B4).
_DEFAULT_OPACITY_SEVERITY_BANDS: tuple[tuple[float, str], ...] = (
    (0.75, "critical"),
    (0.50, "high"),
    (0.25, "medium"),
)

#: Opacity at/above which a resolution needs two approvals (four-eyes). Below it, a single
#: reviewer suffices unless nobody reaches the ownership threshold (then the answer rests on
#: the control ladder and four-eyes applies regardless). Adopter-owned, not a code constant.
_DEFAULT_DUAL_CONTROL_OPACITY: float = 0.50


def _as_opacity_bands(
    value: Any, default: tuple[tuple[float, str], ...]
) -> tuple[tuple[float, str], ...]:
    """Parse ``[[floor, severity], ...]`` into descending ``(float, str)`` bands.

    A malformed or empty override falls back to the default whole, never a partial ladder:
    a half-configured severity ladder is more dangerous than the documented default.
    """
    if not isinstance(value, (list, tuple)) or not value:
        return default
    bands: list[tuple[float, str]] = []
    for entry in value:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            return default
        floor, severity = entry
        if not isinstance(floor, (int, float)) or isinstance(floor, bool):
            return default
        if not isinstance(severity, str) or not severity.strip():
            return default
        bands.append((float(floor), severity.strip().lower()))
    return tuple(sorted(bands, key=lambda band: band[0], reverse=True))


@dataclass(frozen=True)
class UboGraphPolicy:
    """Bank-owned numbers behind the deterministic UBO-graph resolution.

    The engine (``domain/ubo_graph.py``) reads every threshold, limit and weight from
    here, so the beneficial-ownership threshold is the institution's (25% in most EU/UK
    regimes, 10% for some US and higher-risk regimes) rather than a constant buried in
    code (B4). Nothing in the engine hard-codes a percentage.
    """

    #: A natural person at/above this effective percentage is a beneficial owner.
    ownership_threshold_pct: float = 25.0
    #: Effective percentage at/above which a party controls (ladder rung 1).
    control_threshold_pct: float = 50.0
    #: Direct voting percentage at/above which a party controls (ladder rung 2).
    voting_threshold_pct: float = 50.0
    #: Share of the subject's recorded board seats that constitutes control (rung 3).
    board_majority_ratio: float = 0.5
    #: Hops from the subject the walk may take before it truncates.
    max_depth: int = 6
    #: Nodes the walk may admit before it truncates (a runaway-structure guard).
    max_nodes: int = 200
    #: Simple paths enumerated per candidate before the enumeration truncates.
    max_paths: int = 64
    #: Decimal places every percentage is rounded to (byte-identical replays).
    pct_decimals: int = 4
    #: Distinct entities one name must appear against before it reads as a nominee.
    nominee_name_recurrence: int = 3
    #: Name similarity at/above which two recorded names are treated as one party.
    nominee_name_threshold: float = 0.90
    #: Entities sharing one registered address before it reads as a registered agent's.
    shared_address_entities: int = 2
    #: Name tokens that declare a nominee / fiduciary role.
    nominee_tokens: tuple[str, ...] = _DEFAULT_NOMINEE_TOKENS
    #: Single-owner holding at/above this percentage makes a layer a pass-through.
    pass_through_pct: float = 90.0
    #: Days since incorporation below which an entity counts as newly formed.
    young_entity_days: int = 365
    #: Registry filing statuses that read as dormant / struck off.
    dormant_statuses: tuple[str, ...] = _DEFAULT_DORMANT_STATUSES
    #: Distinct shell signals one layer must show before the indicator is raised. Two by
    #: default: a single-shareholder holding company that owns one asset is the most
    #: common lawful corporate structure there is, and flagging it on that trait alone
    #: would bury the real pattern (pass-through AND newly formed AND dormant) in noise.
    min_shell_signals: int = 2
    #: Opacity weight per ``OwnershipFlagKind`` value.
    flag_weight: Mapping[str, float] = field(default_factory=lambda: dict(_DEFAULT_FLAG_WEIGHT))
    #: Descending ``(floor, severity)`` bands mapping opacity score to review severity.
    opacity_severity_bands: tuple[tuple[float, str], ...] = _DEFAULT_OPACITY_SEVERITY_BANDS
    #: Opacity at/above which a resolution requires two approvals (four-eyes).
    dual_control_opacity: float = _DEFAULT_DUAL_CONTROL_OPACITY

    @staticmethod
    def from_mapping(raw: Mapping[str, Any]) -> UboGraphPolicy:
        defaults = UboGraphPolicy()
        weight = raw.get("flag_weight")
        return UboGraphPolicy(
            ownership_threshold_pct=_as_float(
                raw.get("ownership_threshold_pct"), defaults.ownership_threshold_pct
            ),
            control_threshold_pct=_as_float(
                raw.get("control_threshold_pct"), defaults.control_threshold_pct
            ),
            voting_threshold_pct=_as_float(
                raw.get("voting_threshold_pct"), defaults.voting_threshold_pct
            ),
            board_majority_ratio=_as_float(
                raw.get("board_majority_ratio"), defaults.board_majority_ratio
            ),
            max_depth=_as_int(raw.get("max_depth"), defaults.max_depth),
            max_nodes=_as_int(raw.get("max_nodes"), defaults.max_nodes),
            max_paths=_as_int(raw.get("max_paths"), defaults.max_paths),
            pct_decimals=_as_int(raw.get("pct_decimals"), defaults.pct_decimals),
            nominee_name_recurrence=_as_int(
                raw.get("nominee_name_recurrence"), defaults.nominee_name_recurrence
            ),
            nominee_name_threshold=_as_float(
                raw.get("nominee_name_threshold"), defaults.nominee_name_threshold
            ),
            shared_address_entities=_as_int(
                raw.get("shared_address_entities"), defaults.shared_address_entities
            ),
            nominee_tokens=(
                tuple(str(t) for t in raw["nominee_tokens"])
                if raw.get("nominee_tokens")
                else defaults.nominee_tokens
            ),
            pass_through_pct=_as_float(raw.get("pass_through_pct"), defaults.pass_through_pct),
            young_entity_days=_as_int(raw.get("young_entity_days"), defaults.young_entity_days),
            min_shell_signals=_as_int(raw.get("min_shell_signals"), defaults.min_shell_signals),
            dormant_statuses=(
                tuple(str(s) for s in raw["dormant_statuses"])
                if raw.get("dormant_statuses")
                else defaults.dormant_statuses
            ),
            flag_weight=(
                {str(k): float(v) for k, v in weight.items()}
                if isinstance(weight, Mapping)
                else dict(defaults.flag_weight)
            ),
            opacity_severity_bands=_as_opacity_bands(
                raw.get("opacity_severity_bands"), defaults.opacity_severity_bands
            ),
            dual_control_opacity=_as_float(
                raw.get("dual_control_opacity"), defaults.dual_control_opacity
            ),
        )


# --------------------------------------------------------------------------- #
# The aggregate the wiring layer threads into services
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RiskPolicy:
    """Everything under the ``policy:`` key of ``config/settings.yaml``."""

    gap: GapPolicy = field(default_factory=GapPolicy)
    source_of_funds: SofPolicy = field(default_factory=SofPolicy)
    scorecard: ScorecardPolicy = field(default_factory=ScorecardPolicy)
    country_risk: CountryRiskPolicy = field(default_factory=CountryRiskPolicy)
    monitoring: MonitoringPolicy = field(default_factory=MonitoringPolicy)
    escalation: EscalationPolicy = field(default_factory=EscalationPolicy)
    perpetual_kyc: PerpetualKycPolicy = field(default_factory=PerpetualKycPolicy)
    ubo_graph: UboGraphPolicy = field(default_factory=UboGraphPolicy)

    @staticmethod
    def from_mapping(raw: Mapping[str, Any] | None) -> RiskPolicy:
        raw = raw or {}
        return RiskPolicy(
            gap=GapPolicy.from_mapping(raw.get("gap") or {}),
            source_of_funds=SofPolicy.from_mapping(raw.get("source_of_funds") or {}),
            scorecard=ScorecardPolicy.from_mapping(raw.get("scorecard") or {}),
            country_risk=CountryRiskPolicy.from_mapping(raw.get("country_risk") or {}),
            monitoring=MonitoringPolicy.from_mapping(raw.get("monitoring") or {}),
            escalation=EscalationPolicy.from_mapping(raw.get("escalation") or {}),
            perpetual_kyc=PerpetualKycPolicy.from_mapping(raw.get("perpetual_kyc") or {}),
            ubo_graph=UboGraphPolicy.from_mapping(raw.get("ubo_graph") or {}),
        )
