"""Whether invented evidence can ground a case that supplied its own.

The ``local`` profile ships a small fictional corpus so an out-of-the-box CLI run returns a
real cited artifact with no external evidence. Every passage in it was UNTAGGED, and under
the ACL contract untagged means public. So the demo corpus was visible to every case.

Visible was not the damage. Retrieval is capped at ``top_k`` and ordered by relevance, so
the fictional passages did not merely join a case's own evidence -- they competed with it
and, being written to read like model KYC evidence, frequently won. A dossier for a real
subject was then grounded in, and cited, documents nobody had supplied.

It took the paired demonstration to surface it, because on a laptop alone the output looks
correct: a cited, grounded, plausible dossier. Only when the same case ran against the
deployment, which ships no seed corpus, did the two disagree about what the evidence was.

The rule these tests hold: the demo corpus grounds a query that would otherwise retrieve
nothing, and never competes with a case's own evidence for a place in the result.
"""

from __future__ import annotations

from cdd_sow_research.adapters.local._seed import DEMO_CORPUS_TAG, SEED_PASSAGES
from cdd_sow_research.adapters.local.knowledge_base import LocalKnowledgeBaseAdapter
from cdd_sow_research.config import LocalSettings, Settings
from cdd_sow_research.domain.models import DocType, KycDocument, RetrievalQuery

#: Wording chosen to collide with the seed corpus on the terms it indexes. A case document
#: about something the fixtures never mention would pass these tests without exercising the
#: competition that is the actual defect.
_CASE_TEXT = (
    "The audited financial statements show the subject derives income from a majority "
    "shareholding in a logistics business, with annual dividends declared."
)


def _adapter() -> LocalKnowledgeBaseAdapter:
    return LocalKnowledgeBaseAdapter(
        Settings(profile="local", local=LocalSettings(db_path=":memory:", audit_path=":memory:"))
    )


def _ingest_case_document(adapter: LocalKnowledgeBaseAdapter) -> None:
    adapter.ingest(
        KycDocument(id="doc-case-own", doc_type=DocType.FIN_STATEMENT),
        _CASE_TEXT.encode(),
        ("case:meridian",),
        (_CASE_TEXT,),
    )


def test_the_seed_corpus_is_not_public() -> None:
    """Untagged is public. Every seed passage must carry the demo tag instead."""

    assert SEED_PASSAGES, "the corpus must not be empty, or these tests prove nothing"
    for passage in SEED_PASSAGES:
        assert passage.acl_tags == (DEMO_CORPUS_TAG,), passage.citation.source_id


def test_a_case_with_its_own_evidence_is_grounded_only_in_that_evidence() -> None:
    """The defect, stated as a test: no fictional passage may reach a grounded case."""

    adapter = _adapter()
    _ingest_case_document(adapter)

    passages = adapter.search(
        RetrievalQuery(
            text="source of wealth dividends", acl_principals=("case:meridian",), top_k=5
        )
    )

    assert passages, "the case's own document must be retrievable"
    seeded_ids = {p.citation.source_id for p in SEED_PASSAGES}
    cited = {p.citation.source_id for p in passages}
    assert not (cited & seeded_ids), f"invented evidence grounded a real case: {cited & seeded_ids}"
    assert cited == {"doc-case-own"}


def test_the_demo_corpus_still_grounds_a_case_that_supplied_nothing() -> None:
    """The affordance the corpus exists for, and the reason it is a fallback not a deletion.

    Deleting the corpus would also have closed the defect, and would have broken the
    documented out-of-the-box run. This is the half of the behaviour worth keeping.
    """

    passages = _adapter().search(
        RetrievalQuery(text="source of wealth dividends", acl_principals=("case:empty",), top_k=5)
    )

    assert passages, "an ungrounded local run must still find the built-in corpus"
    assert {p.citation.source_id for p in passages} <= {p.citation.source_id for p in SEED_PASSAGES}


def test_the_fallback_does_not_reopen_the_door_for_a_grounded_case() -> None:
    """One retrieved passage is enough to shut it: the fallback is all-or-nothing.

    A fallback that topped a short result set up to ``top_k`` would restore the defect in
    its most confusing form -- fictional evidence appearing only for cases whose real
    evidence was thin, which is exactly when a reviewer is least able to notice.
    """

    adapter = _adapter()
    _ingest_case_document(adapter)

    passages = adapter.search(
        RetrievalQuery(
            text="source of wealth dividends", acl_principals=("case:meridian",), top_k=5
        )
    )

    assert len(passages) == 1, "the case supplied exactly one passage"
    assert passages[0].citation.source_id == "doc-case-own"


def test_holding_the_demo_tag_is_what_admits_the_corpus() -> None:
    """The mechanism, asserted directly, so the fallback cannot be mistaken for a coincidence."""

    adapter = _adapter()
    _ingest_case_document(adapter)

    with_tag = adapter.search(
        RetrievalQuery(
            text="source of wealth dividends",
            acl_principals=("case:meridian", DEMO_CORPUS_TAG),
            top_k=5,
        )
    )

    assert {p.citation.source_id for p in with_tag} & {p.citation.source_id for p in SEED_PASSAGES}


# --------------------------------------------------------------------------------------- #
# The half of the fix that reaches machines which already have the defect.
# --------------------------------------------------------------------------------------- #
def test_an_index_seeded_before_the_corpus_was_scoped_is_repaired_on_open() -> None:
    """Tagging the corpus in source reaches only an index that does not exist yet.

    The corpus is seeded exactly once, when the store is empty, so every laptop that has
    already run this application keeps the untagged -- and therefore public -- rows it was
    seeded with. The source fix would have reached precisely the machines that never had
    the defect and missed every machine that does, which is the worse half of the two:
    nothing looks wrong, because the CLI still prints a cited dossier.

    Reproduced exactly: rows written with no tags, as the old seeding wrote them.
    """

    adapter = _adapter()
    with adapter._lock:  # noqa: SLF001 - reproducing the old on-disk state is the point
        adapter._conn.execute("UPDATE passages SET acl_tags = ''")
        adapter._conn.commit()
    _ingest_case_document(adapter)
    assert adapter._retag_legacy_seed_rows() > 0  # noqa: SLF001

    passages = adapter.search(
        RetrievalQuery(
            text="source of wealth dividends", acl_principals=("case:meridian",), top_k=5
        )
    )

    cited = {p.citation.source_id for p in passages}
    assert not (cited & {p.citation.source_id for p in SEED_PASSAGES})
    assert cited == {"doc-case-own"}


def test_the_repair_leaves_a_case_document_alone() -> None:
    """Matched on seed id AND empty tags, both required.

    The id alone would re-tag a case document that happened to share one; an empty tag set
    alone would capture any legitimately public passage a future profile writes.
    """

    adapter = _adapter()
    _ingest_case_document(adapter)
    with adapter._lock:  # noqa: SLF001
        adapter._conn.execute(
            "UPDATE passages SET acl_tags = '' WHERE source_id = ?", ("doc-case-own",)
        )
        adapter._conn.commit()

    adapter._retag_legacy_seed_rows()  # noqa: SLF001

    with adapter._lock:  # noqa: SLF001
        row = adapter._conn.execute(
            "SELECT acl_tags FROM passages WHERE source_id = ?", ("doc-case-own",)
        ).fetchone()
    assert row["acl_tags"] == "", "a non-seed row must not be re-tagged"


# --------------------------------------------------------------------------------------- #
# The `live` profile: no fallback at all (org decision, 2026-08-30).
# --------------------------------------------------------------------------------------- #
def _live_adapter(db_path: str) -> LocalKnowledgeBaseAdapter:
    return LocalKnowledgeBaseAdapter(
        Settings(profile="live", local=LocalSettings(db_path=db_path, audit_path=":memory:"))
    )


def test_a_live_case_with_no_evidence_is_ungrounded_not_grounded_in_fiction() -> None:
    """Under `live` the demo fallback does not exist: real subjects, no invented grounding.

    The F4 laptop/deployment pair diverged on exactly this — the laptop answered a real
    case from the seeded corpus while the deployment read the case file. An empty result
    here is what makes the pipeline's ungrounded-case hard error reachable, which is the
    correct outcome for a live case that supplied nothing.
    """

    adapter = _live_adapter(":memory:")
    # Plant the tagged demo rows a shared laptop DB may hold.
    adapter.add(list(SEED_PASSAGES[:3]))

    passages = adapter.search(
        RetrievalQuery(text="source of wealth dividends", acl_principals=("case:empty",), top_k=5)
    )

    assert passages == [], "a live case must never be grounded by the demo corpus"


def test_a_live_index_seeded_before_the_corpus_was_scoped_is_repaired_on_open(
    tmp_path,  # noqa: ANN001
) -> None:
    """The on-open repair runs under EVERY profile, not only `local`.

    A laptop's persistent `live` index seeded before the corpus was scoped still holds
    untagged — and therefore public — seed rows, and `live` is exactly the profile where
    they must never compete with a case's own evidence.
    """

    db_path = str(tmp_path / "kb.db")
    seeded = LocalKnowledgeBaseAdapter(
        Settings(profile="local", local=LocalSettings(db_path=db_path, audit_path=":memory:"))
    )
    with seeded._lock:  # noqa: SLF001 - reproducing the old on-disk state is the point
        seeded._conn.execute("UPDATE passages SET acl_tags = ''")
        seeded._conn.commit()

    adapter = _live_adapter(db_path)
    _ingest_case_document(adapter)

    passages = adapter.search(
        RetrievalQuery(
            text="source of wealth dividends", acl_principals=("case:meridian",), top_k=5
        )
    )

    cited = {p.citation.source_id for p in passages}
    assert not (cited & {p.citation.source_id for p in SEED_PASSAGES})
    assert cited == {"doc-case-own"}
