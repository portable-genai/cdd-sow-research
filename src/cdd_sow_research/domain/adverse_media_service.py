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

import logging
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
from .name_match import tokens

_LOG = logging.getLogger(__name__)

_SEVERITY_RANK: dict[Severity, int] = {
    Severity.LOW: 0,
    Severity.MEDIUM: 1,
    Severity.HIGH: 2,
    Severity.CRITICAL: 3,
}


def finding_names_subject(subject_name: str, finding: AdverseMediaFinding) -> bool:
    """Does this negative-news hit actually name the subject, or merely resemble its world?

    A grounded web search returns what is topically near the query, and a model asked for
    "adverse media on X" will hand back the industry and the jurisdiction when X itself has no
    coverage. On 2026-08-26 the deployment did exactly that: asked about a fictional company, it
    returned a real money-laundering prosecution naming real banks, marked it ``critical``, and
    the risk policy turned that into a PROHIBITED band for a subject the article has nothing to
    do with.

    That is the model owning an outcome, which this system's invariants do not allow anywhere.
    So the decision is made here, deterministically, over the text the finding itself carries:
    every distinctive token of the subject's name must appear in it. Legal-form suffixes are
    dropped before comparing, because an article writes "Meridian Harbour" where the register
    writes "Meridian Harbour Holdings Pte Ltd". One shared word is not a match, which is the
    specific hole the real article came through: it shared a jurisdiction and an industry and
    named the subject nowhere.

    Deliberately conservative in the safe direction. Dropping an unverifiable hit costs a
    finding; keeping one costs a subject the most severe band the system can assign, on evidence
    about somebody else.
    """

    core = tokens(subject_name, drop_org_suffixes=True)
    if not core:
        # No distinctive name to match on. Refuse rather than admit everything.
        return False
    # Every distinctive token, not a contiguous run: ``tokens`` drops interior org words, so
    # "Acme Holdings Pte Ltd" reduces to tokens that are not adjacent in any real headline.
    # One shared word is what lets an unrelated article through, and requiring all of them is
    # what stops it.
    haystack = set(tokens(f"{finding.headline} {finding.snippet}", drop_org_suffixes=False))
    return all(token in haystack for token in core)


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
        kept = [f for f in screening.findings if finding_names_subject(subject.name, f)]
        discarded = len(screening.findings) - len(kept)
        if discarded:
            # Never silently. A screen that quietly drops most of what it found looks exactly
            # like a screen that found little, and the count is the only thing that tells them
            # apart afterwards.
            _LOG.warning(
                "adverse media: discarded %d of %d finding(s) for %r that did not name the "
                "subject. A grounded search returns what is topically near the query, and an "
                "unrelated hit would otherwise carry its severity into the risk band.",
                discarded,
                len(screening.findings),
                subject.name,
            )
        findings = [self._ensure_citation(f) for f in kept]
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
