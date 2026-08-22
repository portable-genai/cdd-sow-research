"""AdverseMediaService — negative-news scanning over public-web grounding (SPEC §5).

A thin domain service over the :class:`AdverseMediaPort`: it asks the port for
negative-news hits on the subject's name and returns them ordered by severity (most
severe first) so the risk service and the dossier surface the worst findings prominently.
Each finding carries a :class:`Citation` of ``source_type=MEDIA`` (synthesised from the
finding when the port did not attach one) so every adverse-media claim is provenance
bearing.

Pure domain code: talks only to ports and models, no Google Cloud / ADK imports.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from .models import (
    AdverseMediaFinding,
    AdverseMediaScreening,
    Citation,
    Severity,
    SourceType,
    Subject,
)

_SEVERITY_RANK: dict[Severity, int] = {
    Severity.LOW: 0,
    Severity.MEDIUM: 1,
    Severity.HIGH: 2,
    Severity.CRITICAL: 3,
}


class AdverseMediaService:
    """Scan public-web adverse media for a subject. Signature fixed by SPEC §5."""

    def __init__(self, adverse_media: Any, tracer: Any, max_results: int = 10) -> None:
        self._adverse_media = adverse_media
        self._tracer = tracer
        self._max_results = max_results

    def scan(self, subject: Subject, actor: str) -> AdverseMediaScreening | None:
        """Return the adverse-media screen for ``subject``, most severe first.

        ``None`` means no screen ran: either the port had no reachable backend and said so,
        or it raised. Adverse media is best-effort in the sense that it never fails the
        case, but "best-effort" must not be allowed to mean "silently report clean". The
        previous form caught every exception and returned an empty tuple, which the dossier
        rendered as an affirmative "No adverse media found."
        """
        try:
            screening = self._adverse_media.search(subject.name, max_results=self._max_results)
        except Exception:  # noqa: BLE001 - adverse media is best-effort, never fatal
            return None
        if screening is None:
            return None
        findings = [self._ensure_citation(f) for f in screening.findings]
        findings.sort(key=lambda f: _SEVERITY_RANK.get(f.severity, 1), reverse=True)
        return replace(screening, findings=tuple(findings))

    @staticmethod
    def _ensure_citation(finding: AdverseMediaFinding) -> AdverseMediaFinding:
        if finding.citation is not None:
            return finding
        citation = Citation(
            source_id=f"media:{finding.url or finding.headline}",
            source_type=SourceType.MEDIA,
            title=finding.headline,
            url=finding.url,
            snippet=finding.snippet,
        )
        return replace(finding, citation=citation)
