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

#: The ACL tag every built-in passage carries. A query holds it only via the fallback in
#: ``LocalKnowledgeBaseAdapter.search``, which admits this corpus when the case's own
#: evidence retrieved NOTHING -- so the out-of-the-box CLI smoke run is still grounded, and
#: a case that supplied documents is grounded in those documents and only those.
DEMO_CORPUS_TAG = "demo:seed-corpus"


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
        # Tagged, and deliberately NOT untagged. Untagged means public under the ACL
        # contract, so this fictional corpus was visible to every case: it competed with a
        # case's own uploaded evidence on relevance, outranked it, and -- because
        # retrieval is capped at top_k -- displaced it. Dossiers for a real subject were
        # grounded in invented documents, and cited them, with nothing to show for it.
        #
        # The paired demonstration is what surfaced it: the laptop cited "Bank Statement
        # Records (FICTIONAL)" for a case whose evidence it had just been handed, and the
        # deployment, which has no seed corpus, could not agree with it and never will.
        acl_tags=(DEMO_CORPUS_TAG,),
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
