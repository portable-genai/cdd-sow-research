"""Custody of uploaded case documents: roundtrip, and the fail-closed ACL.

These are the bytes a dossier citation points back at, so the properties that matter are
that what comes out is exactly what went in, and that a caller who does not hold every
one of a document's ACL tags cannot tell it exists.
"""

from __future__ import annotations

import pytest
from tests.fixtures.pdfs import BANK_STATEMENT_PAGES, build_pdf

from cdd_sow_research.adapters.local.document_store import LocalDocumentStoreAdapter
from cdd_sow_research.config import LocalSettings, Settings
from cdd_sow_research.domain.errors import DocumentNotFoundError
from cdd_sow_research.domain.models import DocType

_OWNER = ("group:cdd-analyst", "tenant:demo-bank", "case:meridian")
_OTHER_TENANT = ("group:cdd-analyst", "tenant:other-bank", "case:meridian")
_TAGS = ("case:meridian", "tenant:demo-bank")


@pytest.fixture()
def store() -> LocalDocumentStoreAdapter:
    return LocalDocumentStoreAdapter(
        Settings(profile="live", local=LocalSettings(documents_path=":memory:"))
    )


def _put(store: LocalDocumentStoreAdapter, content: bytes = b"%PDF-1.4 statement"):
    return store.put(
        content=content,
        filename="statement.pdf",
        doc_type=DocType.BANK_STATEMENT,
        subject_id="meridian",
        acl_tags=_TAGS,
        mime_type="application/pdf",
    )


def test_stored_bytes_come_back_byte_identical(store: LocalDocumentStoreAdapter):
    content = build_pdf(BANK_STATEMENT_PAGES)
    record = _put(store, content)

    assert store.get(record.id, _OWNER) == content
    assert record.size_bytes == len(content)
    assert record.sha256 == __import__("hashlib").sha256(content).hexdigest()


def test_metadata_records_the_upload(store: LocalDocumentStoreAdapter):
    record = _put(store)
    loaded = store.metadata(record.id, _OWNER)

    assert loaded.filename == "statement.pdf"
    assert loaded.doc_type is DocType.BANK_STATEMENT
    assert loaded.mime_type == "application/pdf"
    assert loaded.subject_id == "meridian"
    assert loaded.uploaded_at  # ISO-8601 stamp


def test_citation_uri_addresses_the_document_within_its_case(store: LocalDocumentStoreAdapter):
    record = _put(store)
    # Relative, and case-scoped: the serving route derives the ACL from the path before
    # it reads anything.
    assert record.uri == f"/v1/cases/meridian/documents/{record.id}"


def test_a_reader_without_every_tag_cannot_read_or_detect_it(store: LocalDocumentStoreAdapter):
    record = _put(store)

    # Same error as a missing id: the caller cannot distinguish "forbidden" from "absent".
    with pytest.raises(DocumentNotFoundError):
        store.get(record.id, _OTHER_TENANT)
    with pytest.raises(DocumentNotFoundError):
        store.metadata(record.id, _OTHER_TENANT)
    with pytest.raises(DocumentNotFoundError):
        store.get("doc-does-not-exist", _OWNER)
    assert store.list_documents(_OTHER_TENANT) == []


def test_holding_the_case_tag_alone_never_crosses_a_tenant(store: LocalDocumentStoreAdapter):
    record = _put(store)

    with pytest.raises(DocumentNotFoundError):
        store.get(record.id, ("case:meridian",))


def test_list_is_scoped_to_the_case_and_the_reader(store: LocalDocumentStoreAdapter):
    mine = _put(store)
    other = store.put(
        content=b"other case",
        filename="other.pdf",
        doc_type=DocType.OTHER,
        subject_id="apex",
        acl_tags=("case:apex", "tenant:demo-bank"),
        mime_type="application/pdf",
    )

    listed = store.list_documents(_OWNER, subject_id="meridian")

    assert [r.id for r in listed] == [mine.id]
    assert other.id not in [r.id for r in listed]


def test_delete_removes_it_and_is_refused_across_tenants(store: LocalDocumentStoreAdapter):
    record = _put(store)

    with pytest.raises(DocumentNotFoundError):
        store.delete(record.id, _OTHER_TENANT)

    assert store.delete(record.id, _OWNER) is True
    assert store.delete(record.id, _OWNER) is False
    with pytest.raises(DocumentNotFoundError):
        store.get(record.id, _OWNER)


def test_page_count_is_recorded_after_extraction(store: LocalDocumentStoreAdapter):
    record = _put(store)
    assert record.pages == 0

    store.set_pages(record.id, 2)

    assert store.metadata(record.id, _OWNER).pages == 2
