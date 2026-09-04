"""Perpetual-KYC engine: deterministic signal detection, re-scoring and queueing.

Periodic review (``periodic_review_service``) answers *when is this relationship next
due*. Perpetual KYC answers *what changed since we last looked* and re-scores the
relationship the moment it changes. This module is the pure, replayable half of that:

* :func:`signal_key` turns an observed fact into a stable content fingerprint, so the
  same real-world fact yields the same key on every run and a baseline diff is exact
  rather than fuzzy;
* :meth:`PerpetualKycEngine.detect` converts the three monitored edges (sanctions
  screening, adverse media, corporate-registry ownership) into
  :class:`~cdd_sow_research.domain.models.MonitoringSignal` objects, each marked NEW,
  PERSISTING or CLEARED against the stored baseline;
* :meth:`PerpetualKycEngine.rescore` computes the new risk score as
  ``baseline + sum(uplift of NEW signals) - relief(CLEARED signals)``, clamped, with one
  auditable :class:`~cdd_sow_research.domain.models.SignalUplift` line per signal; and
* :meth:`PerpetualKycEngine.queue_item` derives an explainable review-queue place
  (priority, SLA date, reasons, citations) from the re-score.

Every number here is arithmetic over bank-owned policy
(:class:`~cdd_sow_research.domain.policy.PerpetualKycPolicy`). **The LLM never produces a
number in this module and is not imported by it**: it only narrates the finished
assessment elsewhere. Given the same inputs and the same ``as_of`` date, the whole
assessment is byte-identical, so an auditor can recompute it.

A pKYC outcome is consequential decision support: it always sets ``requires_human_review`` and never
auto-blocks. The orchestrator (``perpetual_kyc_service``) routes it to human-review-console for
maker-checker disposition (rule R8).

Pure standard library; no ports, no I/O, no clock of its own (the caller supplies
``as_of``).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta

from .models import (
    AdverseMediaFinding,
    BaselineSignal,
    CddTier,
    Citation,
    HitStatus,
    MonitoringAssessment,
    MonitoringSignal,
    OwnershipSummary,
    PerpetualKycAssessment,
    PerpetualKycBaseline,
    QueuePriority,
    ReviewQueueItem,
    RiskBand,
    ScreeningResult,
    Severity,
    SignalChange,
    SignalSource,
    SignalUplift,
    SourceType,
    Subject,
)
from .policy import PerpetualKycPolicy

#: Order used to rank signals for display and for the worst-severity decision.
_SEVERITY_RANK: dict[Severity, int] = {
    Severity.CRITICAL: 0,
    Severity.HIGH: 1,
    Severity.MEDIUM: 2,
    Severity.LOW: 3,
}

#: Change kinds first in the queue narrative: what moved matters most.
_CHANGE_RANK: dict[SignalChange, int] = {
    SignalChange.NEW: 0,
    SignalChange.CLEARED: 1,
    SignalChange.PERSISTING: 2,
}


def signal_key(source: SignalSource, *parts: str) -> str:
    """A stable fingerprint for one observed fact: ``<source>:<16 hex>``.

    The digest is taken over the normalised identifying parts (case-folded, whitespace
    collapsed), so cosmetic differences in a provider's rendering do not look like a new
    signal, while a genuine change (a different list entry, a changed shareholding) does.
    """
    normalised = "|".join(" ".join(part.split()).casefold() for part in parts)
    digest = hashlib.sha256(f"{source.value}|{normalised}".encode()).hexdigest()[:16]
    return f"{source.value}:{digest}"


def _source_of(key: str) -> SignalSource:
    """The signal family a key belongs to (falls back to REGISTRY for unknown keys)."""
    prefix = key.split(":", 1)[0]
    try:
        return SignalSource(prefix)
    except ValueError:
        return SignalSource.REGISTRY


def _at(as_of: date) -> datetime:
    """The single timestamp a run stamps on everything it produces (replayability)."""
    return datetime.combine(as_of, time.min, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class PerpetualKycEngine:
    """Pure, deterministic perpetual-KYC detection, re-scoring and queue placement."""

    #: Uplift per NEW signal keyed by ``Severity`` value (bank-owned policy).
    severity_uplift: Mapping[str, float] = field(
        default_factory=lambda: dict(PerpetualKycPolicy().severity_uplift)
    )
    #: Multiplier per ``SignalSource`` value applied to the severity uplift.
    source_weight: Mapping[str, float] = field(
        default_factory=lambda: dict(PerpetualKycPolicy().source_weight)
    )
    max_uplift: float = 0.50
    cleared_relief: float = 0.50
    urgent_score: float = 0.75
    high_score: float = 0.50
    low_score: float = 0.25
    #: Days allowed to disposition a queued review keyed by ``QueuePriority`` value.
    sla_days: Mapping[str, int] = field(default_factory=lambda: dict(PerpetualKycPolicy().sla_days))

    @classmethod
    def from_policy(cls, policy: PerpetualKycPolicy) -> PerpetualKycEngine:
        """Build the engine from the bank-owned policy object (settings-driven)."""
        return cls(
            severity_uplift=dict(policy.severity_uplift),
            source_weight=dict(policy.source_weight),
            max_uplift=policy.max_uplift,
            cleared_relief=policy.cleared_relief,
            urgent_score=policy.urgent_score,
            high_score=policy.high_score,
            low_score=policy.low_score,
            sla_days=dict(policy.sla_days),
        )

    # ------------------------------------------------------------------ #
    # 1. Detection
    # ------------------------------------------------------------------ #
    def detect(
        self,
        *,
        as_of: date,
        baseline: PerpetualKycBaseline | None = None,
        screening: ScreeningResult | None = None,
        adverse_media: Sequence[AdverseMediaFinding] = (),
        ownership: OwnershipSummary | None = None,
    ) -> tuple[MonitoringSignal, ...]:
        """Observe the monitored edges and diff them against the baseline.

        On the FIRST run (no baseline) every observed signal is PERSISTING, not NEW: the
        run establishes what "unchanged" means, and the standing risk of the current
        picture is already carried by the case's own scorecard. Only movement after that
        point re-scores the relationship.
        """
        known = frozenset(baseline.signal_keys if baseline else ())
        first_run = baseline is None
        observed = [
            *self._sanctions_signals(screening, as_of=as_of),
            *self._media_signals(adverse_media, as_of=as_of),
            *self._registry_signals(ownership, as_of=as_of),
        ]

        # De-duplicate on key, keeping the first (most severe within its family).
        deduped: dict[str, MonitoringSignal] = {}
        for signal in observed:
            deduped.setdefault(signal.key, signal)

        signals: list[MonitoringSignal] = []
        for signal in deduped.values():
            change = (
                SignalChange.PERSISTING if first_run or signal.key in known else SignalChange.NEW
            )
            signals.append(_replace_change(signal, change))

        # Anything the baseline knew about and we no longer observe has cleared. The
        # baseline remembers what each signal weighed, so the relief is proportionate.
        for key in sorted(known - set(deduped)):
            remembered = baseline.signal_for(key) if baseline is not None else None
            source = remembered.source if remembered is not None else _source_of(key)
            signals.append(
                MonitoringSignal(
                    key=key,
                    source=source,
                    change=SignalChange.CLEARED,
                    severity=remembered.severity if remembered is not None else Severity.LOW,
                    summary=f"A {source.value.replace('_', ' ')} signal is no longer observed.",
                    detail=(
                        f"Signal {key} was present at the {baseline.as_of if baseline else ''} "
                        "baseline and is absent from the current observation."
                    ),
                    observed_at=_at(as_of),
                )
            )
        return tuple(sorted(signals, key=self._signal_order))

    def _sanctions_signals(
        self, screening: ScreeningResult | None, *, as_of: date
    ) -> list[MonitoringSignal]:
        """One signal per open watchlist alert, cited to the list snapshot version."""
        if screening is None:
            return []
        out: list[MonitoringSignal] = []
        for alert in screening.open_alerts:
            entry = alert.match.entry
            key = signal_key(
                SignalSource.SANCTIONS, entry.source.value, entry.uid, alert.match.matched_name
            )
            severity = (
                Severity.CRITICAL if alert.status is HitStatus.TRUE_POSITIVE else Severity.HIGH
            )
            out.append(
                MonitoringSignal(
                    key=key,
                    source=SignalSource.SANCTIONS,
                    change=SignalChange.PERSISTING,
                    severity=severity,
                    summary=(
                        f"Watchlist match on {entry.source.value} "
                        f"(score {alert.match.score:.2f}, status {alert.status.value})."
                    ),
                    detail=(
                        f"Matched '{alert.match.matched_name}' in {entry.source.value} "
                        f"entry {entry.uid}; features: {', '.join(alert.match.features) or 'none'}."
                    ),
                    citation=Citation(
                        source_id=f"watchlist:{entry.source.value}:{entry.uid}",
                        source_type=SourceType.REGISTRY,
                        title=f"{entry.source.value} entry {entry.uid}",
                        snippet=entry.remark or entry.name,
                    ),
                    source_version=screening.lists_version,
                    observed_at=_at(as_of),
                )
            )
        return out

    def _media_signals(
        self, findings: Sequence[AdverseMediaFinding], *, as_of: date
    ) -> list[MonitoringSignal]:
        """One signal per adverse-media finding, carrying the finding's own citation."""
        out: list[MonitoringSignal] = []
        for finding in findings:
            key = signal_key(
                SignalSource.ADVERSE_MEDIA,
                finding.category.value,
                finding.url or finding.headline,
            )
            out.append(
                MonitoringSignal(
                    key=key,
                    source=SignalSource.ADVERSE_MEDIA,
                    change=SignalChange.PERSISTING,
                    severity=finding.severity,
                    summary=f"Adverse media ({finding.category.value}): {finding.headline}",
                    detail=finding.snippet or finding.headline,
                    citation=finding.citation
                    or Citation(
                        source_id=f"media:{finding.url or finding.headline}",
                        source_type=SourceType.MEDIA,
                        title=finding.headline,
                        url=finding.url,
                        snippet=finding.snippet,
                    ),
                    source_version=finding.published_date or "",
                    observed_at=_at(as_of),
                )
            )
        return out

    def _registry_signals(
        self, ownership: OwnershipSummary | None, *, as_of: date
    ) -> list[MonitoringSignal]:
        """One signal per beneficial owner, keyed on name + rounded percentage.

        Keying on the shareholding means a change in control (a new owner, a departed
        owner, a moved percentage) shows up as a CLEARED/NEW pair rather than silently
        overwriting the previous picture.
        """
        if ownership is None:
            return []
        out: list[MonitoringSignal] = []
        for owner in ownership.owners:
            pct = f"{owner.pct:.2f}"
            key = signal_key(SignalSource.REGISTRY, ownership.root_entity, owner.name, pct)
            severity = Severity.HIGH if owner.is_pep else Severity.MEDIUM
            citation = owner.citations[0] if owner.citations else None
            out.append(
                MonitoringSignal(
                    key=key,
                    source=SignalSource.REGISTRY,
                    change=SignalChange.PERSISTING,
                    severity=severity,
                    summary=(
                        f"Registry shows {owner.name} holding {pct}% of "
                        f"{ownership.root_entity}"
                        + (" (politically-exposed person)." if owner.is_pep else ".")
                    ),
                    detail=(
                        f"Beneficial owner {owner.name}"
                        + (f", country {owner.country}" if owner.country else "")
                        + f", holding {pct}%."
                    ),
                    citation=citation
                    or Citation(
                        source_id=f"registry:{ownership.root_entity}:{owner.name}",
                        source_type=SourceType.REGISTRY,
                        title=f"Corporate registry extract for {ownership.root_entity}",
                        snippet=f"{owner.name} holds {pct}%.",
                    ),
                    observed_at=_at(as_of),
                )
            )
        return out

    @staticmethod
    def _signal_order(signal: MonitoringSignal) -> tuple[int, int, str]:
        return (
            _CHANGE_RANK.get(signal.change, 9),
            _SEVERITY_RANK.get(signal.severity, 9),
            signal.key,
        )

    # ------------------------------------------------------------------ #
    # 2. Re-scoring
    # ------------------------------------------------------------------ #
    def rescore(
        self,
        baseline_score: float,
        signals: Sequence[MonitoringSignal],
    ) -> tuple[float, tuple[SignalUplift, ...]]:
        """Return the re-scored total plus one auditable uplift line per signal.

        NEW signals raise the score by ``severity_uplift * source_weight``; CLEARED
        signals return ``cleared_relief`` of that amount. The summed positive uplift is
        capped at ``max_uplift`` and the final score is clamped to ``[0, 1]``.
        """
        uplifts: list[SignalUplift] = []
        raised = 0.0
        relieved = 0.0
        for signal in signals:
            unit = self._unit_uplift(signal)
            if signal.change is SignalChange.NEW:
                raised += unit
                uplifts.append(
                    SignalUplift(
                        key=signal.key,
                        source=signal.source,
                        change=signal.change,
                        severity=signal.severity,
                        uplift=round(unit, 4),
                        reason=(
                            f"new {signal.source.value} signal at {signal.severity.value} "
                            f"severity raises the score by {unit:.4f}"
                        ),
                    )
                )
            elif signal.change is SignalChange.CLEARED:
                relief = unit * self.cleared_relief
                relieved += relief
                uplifts.append(
                    SignalUplift(
                        key=signal.key,
                        source=signal.source,
                        change=signal.change,
                        severity=signal.severity,
                        uplift=round(-relief, 4),
                        reason=(
                            f"cleared {signal.source.value} signal returns "
                            f"{relief:.4f} of its uplift"
                        ),
                    )
                )
            else:
                uplifts.append(
                    SignalUplift(
                        key=signal.key,
                        source=signal.source,
                        change=signal.change,
                        severity=signal.severity,
                        uplift=0.0,
                        reason="unchanged since the baseline; no re-score contribution",
                    )
                )
        capped = min(raised, self.max_uplift)
        total = max(0.0, min(1.0, baseline_score + capped - relieved))
        return round(total, 4), tuple(uplifts)

    def _unit_uplift(self, signal: MonitoringSignal) -> float:
        severity = self.severity_uplift.get(signal.severity.value, 0.0)
        weight = self.source_weight.get(signal.source.value, 1.0)
        return severity * weight

    def band_tier(
        self, score: float, signals: Sequence[MonitoringSignal]
    ) -> tuple[RiskBand, CddTier]:
        """Map a re-scored total plus hard signals to a band and CDD tier.

        Hard signals dominate the arithmetic exactly as they do in the scorecard: an open
        watchlist match is PROHIBITED/EDD regardless of the weighted total.
        """
        if any(
            s.source is SignalSource.SANCTIONS and s.change is not SignalChange.CLEARED
            for s in signals
        ):
            return RiskBand.PROHIBITED, CddTier.EDD
        if score >= self.high_score:
            return RiskBand.HIGH, CddTier.EDD
        if score < self.low_score:
            return RiskBand.LOW, CddTier.SDD
        return RiskBand.MEDIUM, CddTier.CDD

    # ------------------------------------------------------------------ #
    # 3. Queue placement
    # ------------------------------------------------------------------ #
    def priority(self, score: float, signals: Sequence[MonitoringSignal]) -> QueuePriority:
        """Deterministic queue priority: hard signals first, then the re-scored total."""
        new_signals = [s for s in signals if s.change is SignalChange.NEW]
        if any(s.source is SignalSource.SANCTIONS for s in new_signals):
            return QueuePriority.URGENT
        if score >= self.urgent_score:
            return QueuePriority.URGENT
        if any(s.severity is Severity.CRITICAL for s in new_signals):
            return QueuePriority.URGENT
        if score >= self.high_score or any(s.severity is Severity.HIGH for s in new_signals):
            return QueuePriority.HIGH
        if score < self.low_score and not new_signals:
            return QueuePriority.LOW
        return QueuePriority.STANDARD

    def queue_item(
        self,
        *,
        subject: Subject,
        as_of: date,
        score: float,
        score_delta: float,
        signals: Sequence[MonitoringSignal],
        monitoring: MonitoringAssessment | None = None,
    ) -> ReviewQueueItem:
        """Build the explainable queue entry a reviewer opens (reasons + citations)."""
        priority = self.priority(score, signals)
        days = int(self.sla_days.get(priority.value, 15))
        reasons: list[str] = []
        for signal in signals:
            if signal.change is SignalChange.PERSISTING:
                continue
            reasons.append(f"[{signal.change.value}] {signal.summary}")
        if score_delta:
            direction = "increased" if score_delta > 0 else "decreased"
            reasons.append(
                f"Risk score {direction} by {abs(score_delta):.4f} to {score:.4f} "
                "(deterministic re-score over bank policy weights)."
            )
        if monitoring is not None and monitoring.triggers:
            reasons.extend(f"[trigger] {t.summary}" for t in monitoring.triggers)
        if not reasons:
            reasons.append("No change since the baseline; queued for scheduled confirmation.")
        citations = tuple(
            c
            for c in (s.citation for s in signals if s.change is not SignalChange.CLEARED)
            if c is not None
        )
        deduped: dict[str, Citation] = {}
        for citation in citations:
            deduped.setdefault(citation.source_id, citation)
        return ReviewQueueItem(
            id=f"pkyc-{subject.id}-{as_of.isoformat()}",
            subject_id=subject.id,
            tenant=subject.tenant,
            priority=priority,
            sla_due=(as_of + timedelta(days=days)).isoformat(),
            reasons=tuple(reasons),
            citations=tuple(deduped.values()),
            requires_human_review=True,
        )

    # ------------------------------------------------------------------ #
    # 4. The whole assessment
    # ------------------------------------------------------------------ #
    def assess(
        self,
        *,
        subject: Subject,
        as_of: date,
        baseline: PerpetualKycBaseline | None = None,
        screening: ScreeningResult | None = None,
        adverse_media: Sequence[AdverseMediaFinding] = (),
        ownership: OwnershipSummary | None = None,
        monitoring: MonitoringAssessment | None = None,
        acl: tuple[str, ...] = (),
    ) -> PerpetualKycAssessment:
        """Detect, re-score and queue in one replayable step (no I/O, no clock)."""
        signals = self.detect(
            as_of=as_of,
            baseline=baseline,
            screening=screening,
            adverse_media=adverse_media,
            ownership=ownership,
        )
        baseline_score = baseline.score if baseline else 0.0
        baseline_band = baseline.band if baseline else RiskBand.LOW
        score, uplifts = self.rescore(baseline_score, signals)
        band, tier = self.band_tier(score, signals)
        delta = round(score - baseline_score, 4)
        queue_item = self.queue_item(
            subject=subject,
            as_of=as_of,
            score=score,
            score_delta=delta,
            signals=signals,
            monitoring=monitoring,
        )
        new_count = sum(1 for s in signals if s.change is SignalChange.NEW)
        cleared_count = sum(1 for s in signals if s.change is SignalChange.CLEARED)
        rationale = (
            f"Perpetual-KYC re-score for {subject.id} as at {as_of.isoformat()}: "
            f"{new_count} new and {cleared_count} cleared signal(s) moved the score from "
            f"{baseline_score:.4f} to {score:.4f} "
            f"({baseline_band.value.upper()} -> {band.value.upper()}, tier {tier.value.upper()}). "
            f"Priority {queue_item.priority.value.upper()}, "
            f"disposition due {queue_item.sla_due}."
        )
        return PerpetualKycAssessment(
            subject_id=subject.id,
            subject_name=subject.name,
            tenant=subject.tenant,
            as_of=as_of.isoformat(),
            signals=signals,
            uplifts=uplifts,
            baseline_score=round(baseline_score, 4),
            baseline_band=baseline_band,
            score=score,
            score_delta=delta,
            band=band,
            tier=tier,
            rationale=rationale,
            monitoring=monitoring,
            queue_item=queue_item,
            lists_version=screening.lists_version if screening else "",
            requires_human_review=True,
            acl=acl,
            generated_at=_at(as_of),
        )

    @staticmethod
    def next_baseline(assessment: PerpetualKycAssessment) -> PerpetualKycBaseline:
        """The baseline this run establishes for the next one (CLEARED signals dropped)."""
        remembered = tuple(
            sorted(
                (
                    BaselineSignal(key=s.key, source=s.source, severity=s.severity)
                    for s in assessment.signals
                    if s.change is not SignalChange.CLEARED
                ),
                key=lambda s: s.key,
            )
        )
        return PerpetualKycBaseline(
            subject_id=assessment.subject_id,
            tenant=assessment.tenant,
            as_of=assessment.as_of,
            signals=remembered,
            score=assessment.score,
            band=assessment.band,
            tier=assessment.tier,
            acl=assessment.acl,
        )


def _replace_change(signal: MonitoringSignal, change: SignalChange) -> MonitoringSignal:
    from dataclasses import replace

    return replace(signal, change=change)
