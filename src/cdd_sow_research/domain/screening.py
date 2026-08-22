"""ScreeningService — deterministic sanctions/PEP/watchlist name screening.

Screens a subject (or any name + optional DOB) against a *point-in-time* watchlist
snapshot supplied by the :class:`SanctionsListProviderPort` (OFAC SDN + Consolidated,
UN, EU, UK HMT, PEP). Matching is the pure :mod:`name_match` engine, so a hit is
reproducible and an auditor can recompute it; the LLM is not involved. Each hit becomes
a :class:`ScreeningAlert` an analyst dispositions (true/false positive) under four-eyes.

Disposition is **soft**: any open alert escalates the case to enhanced review, but never
auto-blocks — the checker disposes (maker-checker, P-06).
"""

from __future__ import annotations

from dataclasses import dataclass

from ..ports.screening import SanctionsListProviderPort
from . import name_match as nm
from .models import (
    ListSource,
    ScreeningAlert,
    ScreeningMatch,
    ScreeningResult,
    Subject,
    WatchlistEntry,
)


@dataclass(frozen=True, slots=True)
class ScreeningService:
    """Pure, deterministic name screening. Threshold configurable (OFAC-style fuzzy)."""

    #: Combined name(+DOB) similarity at/above which a watchlist entry raises an alert.
    threshold: float = 0.85

    def screen_subject(
        self, subject: Subject, provider: SanctionsListProviderPort
    ) -> ScreeningResult:
        return self.screen(subject.id, subject.name, subject.dob_or_incorp, provider)

    def screen(
        self,
        subject_id: str,
        name: str,
        dob: str | None,
        provider: SanctionsListProviderPort,
    ) -> ScreeningResult:
        """Screen ``name`` (+ optional ``dob``) against the provider's snapshot."""
        version = provider.version()
        sources: set[ListSource] = set()
        alerts: list[ScreeningAlert] = []
        for entry in provider.iter_entries():
            sources.add(entry.source)
            match = self._match(name, dob, entry)
            if match is not None:
                alerts.append(
                    ScreeningAlert(
                        id=f"alert-{subject_id}-{entry.source.value}-{entry.uid}",
                        subject_id=subject_id,
                        match=match,
                    )
                )
        alerts.sort(key=lambda a: a.match.score, reverse=True)
        return ScreeningResult(
            subject_id=subject_id,
            query_name=name,
            lists_version=version,
            sources=tuple(sorted(sources, key=lambda s: s.value)),
            alerts=tuple(alerts),
        )

    # ------------------------------------------------------------------ #
    def _match(self, name: str, dob: str | None, entry: WatchlistEntry) -> ScreeningMatch | None:
        score, matched = nm.best_name_score(name, [entry.name, *entry.aliases])
        if score <= 0.0:
            return None
        features = [f"name {score:.2f}"]
        combined = score
        agree = nm.dob_agreement(dob, entry.dob)
        if agree == 1.0:
            combined = min(1.0, score + 0.05)
            features.append("dob exact")
        elif agree == 0.5:
            features.append("dob year match")
        elif agree == 0.0:
            combined = score * 0.85  # DOB conflict discounts an otherwise-close name
            features.append("dob conflict")
        if entry.countries:
            features.append("country " + "/".join(entry.countries[:2]))
        if combined < self.threshold:
            return None
        return ScreeningMatch(
            entry=entry,
            score=round(combined, 4),
            matched_name=matched,
            features=tuple(features),
        )


@dataclass(frozen=True, slots=True)
class ScreeningPolicy:
    """Soft maker-checker policy for screening alerts (P-06)."""

    def requires_enhanced_review(self, result: ScreeningResult | None) -> bool:
        """True if any alert is still open (pending or confirmed true positive)."""
        return bool(result and result.escalates)
