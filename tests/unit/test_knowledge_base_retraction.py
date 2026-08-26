"""Evidence must be able to leave retrieval, not only enter it.

``KnowledgeBaseClientPort`` carried ``ingest`` and ``search`` and nothing else until
2026-08-26, so an indexed passage was permanent. The custody store has always had ``delete``,
which made the two halves disagree in the dangerous direction: removing a document from custody
left its passages indexed and citable, and retrieval is the half a dossier actually quotes. That
is how 21 pre-fix duplicate copies of one bank statement survived a repair that reported success.

It is more than a cleanup gap. A CDD system that cannot retract an indexed document cannot
honour an erasure request, cannot withdraw evidence filed against the wrong case, and cannot
correct a document it later learns is forged -- while continuing to cite all three.

These run against the real local adapter and a real SQLite store, because the assertion worth
making is that a retracted passage stops coming back from ``search``.
"""

from __future__ import annotations

import pytest

from cdd_sow_research.adapters.local.knowledge_base import LocalKnowledgeBaseAdapter
from cdd_sow_research.domain.models import DocType, KycDocument, RetrievalQuery

_TAGS = ("case:acme", "tenant:reference-bank")
_TEXT = b"Meridian sale consideration SGD 12,400,000 received 2019-11-04\n"


def _adapter(tmp_path, settings_factory):
    return settings_factory(tmp_path)


def _ingest(kb, doc_id: str, tags=_TAGS) -> None:
    kb.ingest(
        KycDocument(id=doc_id, doc_type=DocType.BANK_STATEMENT, uri=f"/d/{doc_id}"),
        _TEXT,
        tags,
    )


def _search(kb, principals=_TAGS):
    return kb.search(
        RetrievalQuery(text="Meridian sale consideration", acl_principals=principals, top_k=10)
    )


@pytest.fixture
def kb(tmp_path, monkeypatch):
    monkeypatch.setenv("CDD_LOCAL_DB", str(tmp_path / "kb.db"))
    from cdd_sow_research.config import Settings

    return LocalKnowledgeBaseAdapter(Settings.load())


def test_a_retracted_document_stops_being_retrievable(kb) -> None:
    _ingest(kb, "doc-keep")
    _ingest(kb, "doc-retract")
    assert {p.citation.source_id for p in _search(kb)} == {"doc-keep", "doc-retract"}

    assert kb.retract("doc-retract", _TAGS) is True

    remaining = {p.citation.source_id for p in _search(kb)}
    assert remaining == {"doc-keep"}, "a retracted passage must not come back from search"


def test_retracting_what_is_not_there_is_false_and_not_an_error(kb) -> None:
    """A repair that runs twice must be as safe as one that runs once."""
    _ingest(kb, "doc-keep")

    assert kb.retract("doc-never-existed", _TAGS) is False
    assert kb.retract("doc-keep", _TAGS) is True
    assert kb.retract("doc-keep", _TAGS) is False


def test_a_caller_who_cannot_read_the_document_cannot_retract_it(kb) -> None:
    """A retraction is a write against evidence; it gets the same fail-closed check as a read."""
    _ingest(kb, "doc-other-tenant", ("case:other", "tenant:someone-else"))

    with pytest.raises(PermissionError):
        kb.retract("doc-other-tenant", _TAGS)

    # And it is still there, which is the half that matters.
    assert kb.retract("doc-other-tenant", ("case:other", "tenant:someone-else")) is True
