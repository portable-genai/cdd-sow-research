"""What a cited document is CALLED, and whether both stores call it the same thing.

The paired demonstration (``journey-portal``'s ``make e2e-pair``) compares the dossier the
laptop produces against the dossier the deployment produces. On its first run every managed
citation title had decayed into the citation's own document id: a source of wealth grounded
in ``doc-c9dba9861a1f`` where the laptop cited ``bank_statement``. Eight of eight.

The link still resolved, so nothing failed. That is the whole difficulty: an evidence
relationship can degrade completely while every check stays green, because "a citation
exists and points somewhere" is true either way. What a reviewer needs from a citation is
the NAME of the thing they are being asked to trust.

Two defects, one on each side of the store:

* ``ingest`` never wrote a title into ``struct_data``, and a search response carries the
  document's id and its ``struct_data`` and nothing else -- so a title absent at ingest is
  a title that can never be read back, whatever the search side does.
* ``search`` read ``struct.get("source_id")`` into the title field. Even had a title been
  written, it would have been ignored.

These tests are the guard for both halves, and for the property that actually matters: the
two profiles must produce the SAME title for the same document, because the title is the
only stable cross-profile identity a citation has. Ids are minted per store and can never
match.
"""

from __future__ import annotations

from typing import Any

from cdd_sow_research.adapters.gcp.agent_search_kb import (
    AgentSearchKnowledgeBaseAdapter,
    _struct_data,
)
from cdd_sow_research.adapters.local.knowledge_base import LocalKnowledgeBaseAdapter
from cdd_sow_research.config import LocalSettings, Settings
from cdd_sow_research.domain.models import DocType, KycDocument, RetrievalQuery, citation_title


def _settings() -> Settings:
    return Settings(profile="local", local=LocalSettings(db_path=":memory:", audit_path=":memory:"))


class _FakeSearchResult:
    """The shape ``_result_to_passages`` reads: a document with struct and derived struct.

    Deliberately a stand-in for the SEARCH RESPONSE and not for the adapter: what is under
    test is the adapter's mapping of a response into citations, so the response is the
    input and the adapter is the code being exercised.
    """

    def __init__(self, struct: dict[str, Any], segments: list[dict[str, Any]]) -> None:
        self.document = type(
            "_Doc",
            (),
            {
                "struct_data": struct,
                "derived_struct_data": {"extractive_segments": segments},
                "id": str(struct.get("source_id", "")),
            },
        )()


_SEGMENTS = [{"content": "Proceeds of the 2019 asset sale.", "pageIdentifier": "3"}]


def _managed_struct(document: KycDocument, acl_tags: tuple[str, ...] = ()) -> dict[str, Any]:
    """Exactly what the managed adapter's ``ingest`` writes beside a document.

    ``_struct_data`` is the whole content of the ingest half, factored out for the same
    reason ``_ingest_mime_type`` was: the Google SDK is not installed on this path, and a
    test that hand-writes the struct would assert what it believes ingest writes rather
    than what ingest writes.
    """
    return _struct_data(document, acl_tags, page_count=1)


def test_the_managed_store_writes_a_title_at_ingest() -> None:
    """Absent this, no search-side fix can work: the name is simply not in the store."""

    document = KycDocument(id="doc-c9dba9861a1f", doc_type=DocType.BANK_STATEMENT)
    struct = _managed_struct(document)

    assert struct["title"] == "bank_statement"
    assert struct["title"] != struct["source_id"]


def test_a_managed_citation_is_named_by_the_document_not_by_its_id() -> None:
    """The exact defect the pair caught: title == source_id, eight times out of eight."""

    document = KycDocument(id="doc-c9dba9861a1f", doc_type=DocType.BANK_STATEMENT)
    adapter = AgentSearchKnowledgeBaseAdapter(_settings())

    passages = adapter._result_to_passages(_FakeSearchResult(_managed_struct(document), _SEGMENTS))

    assert passages, "a result carrying a segment must yield a passage"
    citation = passages[0].citation
    assert citation.title == "bank_statement"
    assert citation.title != citation.source_id


def test_both_profiles_call_the_same_document_by_the_same_name() -> None:
    """The property the pair actually asserts, and the reason the helper is shared.

    Comparing the two adapters' output directly is the point: a fix applied to one side
    only would leave the pair red for a reason that reads like policy divergence.
    """

    document = KycDocument(id="doc-local-side", doc_type=DocType.BANK_STATEMENT)
    local = LocalKnowledgeBaseAdapter(_settings())
    local_passages = local.ingest(
        document,
        b"Proceeds of the 2019 asset sale.",
        ("case:meridian",),
        ("Proceeds of the 2019 asset sale.",),
    )
    assert local_passages.ok

    results = local.search(
        RetrievalQuery(text="asset sale", acl_principals=("case:meridian",), top_k=5)
    )
    assert results, "the local store must return the passage it just ingested"

    managed = AgentSearchKnowledgeBaseAdapter(_settings())
    managed_passages = managed._result_to_passages(
        _FakeSearchResult(_managed_struct(document, ("case:meridian",)), _SEGMENTS)
    )

    assert results[0].citation.title == managed_passages[0].citation.title


def test_a_document_ingested_before_titles_existed_is_named_by_its_kind() -> None:
    """Back-compatibility that is not a silent fallback.

    A document already in the managed store carries no ``title`` key. Reading its
    ``doc_type`` names it correctly; falling straight back to the id would keep the defect
    alive for exactly the corpus that has it.
    """

    adapter = AgentSearchKnowledgeBaseAdapter(_settings())
    legacy = {"source_id": "doc-old", "doc_type": "passport", "uri": "", "acl_tags": []}

    passages = adapter._result_to_passages(_FakeSearchResult(legacy, _SEGMENTS))

    assert passages[0].citation.title == "passport"


def test_only_a_document_carrying_neither_is_reduced_to_its_id() -> None:
    """And then the citation says so, rather than presenting an id as though it were a name."""

    adapter = AgentSearchKnowledgeBaseAdapter(_settings())
    nameless = {"source_id": "doc-nameless", "uri": "", "acl_tags": []}

    passages = adapter._result_to_passages(_FakeSearchResult(nameless, _SEGMENTS))

    assert passages[0].citation.title == "doc-nameless"


def test_the_title_helper_is_the_single_source_of_the_name() -> None:
    """A regression guard for the drift itself, not for either adapter's copy of it."""

    assert citation_title(DocType.BANK_STATEMENT) == "bank_statement"
    assert citation_title(DocType.REGISTRY_EXTRACT) == "registry_extract"
