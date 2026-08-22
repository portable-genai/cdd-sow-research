"""Built-in synthetic case corpus for the ``local`` profile.

A tiny, clearly-fictional set of KYC-pack evidence passages (with page-level citations)
so the local knowledge-base adapter has something to ground a dossier on out of the box,
and the end-to-end CLI smoke run returns a real cited artifact with no external corpus.
The text is invented; the subject names / source ids are plausible but fictional and
must never be treated as real customer data.

This lives under ``src`` (not ``tests``) so the shipped package can seed itself without
importing the test tree; it deliberately mirrors the shape of ``tests/fixtures``.
"""

from __future__ import annotations

from ...domain.models import (
    Citation,
    RetrievedPassage,
    SourceType,
)


def _passage(
    *,
    source_id: str,
    source_type: SourceType,
    title: str,
    page: int,
    text: str,
    score: float,
) -> RetrievedPassage:
    return RetrievedPassage(
        text=text,
        citation=Citation(
            source_id=source_id,
            source_type=source_type,
            title=title,
            url=f"https://example.test/{source_id}",
            page=page,
            snippet=text[:120],
            score=score,
        ),
        score=score,
        # Untagged: the built-in corpus is visible to any case so an out-of-the-box
        # local run is grounded; ingested case documents carry their own case ACL tags.
        acl_tags=(),
    )


# A small, deterministic corpus. Page numbers are required for CDD provenance.
SEED_PASSAGES: tuple[RetrievedPassage, ...] = (
    _passage(
        source_id="doc-financials",
        source_type=SourceType.DOCUMENT,
        title="Audited Financial Statements (FICTIONAL)",
        page=4,
        text=(
            "The audited financial statements show the subject derives income from a "
            "majority shareholding in a profitable logistics business, with annual "
            "dividends in the USD 1m-5m band."
        ),
        score=0.94,
    ),
    _passage(
        source_id="doc-registry",
        source_type=SourceType.REGISTRY,
        title="Corporate Registry Extract (FICTIONAL)",
        page=2,
        text=(
            "The corporate registry extract lists the ultimate beneficial owner as a "
            "single natural person holding 75 percent, with the remaining 25 percent "
            "held by a family trust."
        ),
        score=0.90,
    ),
    _passage(
        source_id="doc-bank",
        source_type=SourceType.DOCUMENT,
        title="Bank Statement Records (FICTIONAL)",
        page=11,
        text=(
            "An earlier asset sale of a residential property contributed a one-off gain, "
            "corroborated by the bank statement records on file."
        ),
        score=0.82,
    ),
)
