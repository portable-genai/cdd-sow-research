"""An uploaded document must end up cited, by page, and openable.

This is the property the whole feature exists for: a real file goes into custody, its
text grounds the dossier, and every citation it produces names the page it came from and
resolves to a URL that serves those exact bytes back. It runs on the local adapters (no
model server), because the plumbing is what is under test.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from tests.conftest import (
    FakeAdverseMedia,
    FakeAudit,
    FakeCompliance,
    FakeExtraction,
    FakeGuardrail,
    FakeKnowledgeBase,
    FakeLLM,
    FakeRedaction,
    FakeRegistry,
    FakeTracer,
)
from tests.fixtures.pdfs import BANK_STATEMENT_PAGES, build_pdf

from cdd_sow_research.adapters.local.document_store import LocalDocumentStoreAdapter
from cdd_sow_research.config import LocalSettings, Settings
from cdd_sow_research.domain.errors import RetrievalEmptyError
from cdd_sow_research.domain.models import (
    CaseInput,
    DocType,
    RetrievalQuery,
    Subject,
    SubjectType,
)
from cdd_sow_research.domain.services import CddService

_TENANT = "demo-bank"
_PRINCIPALS = ("group:cdd-analyst",)
_SUBJECT = Subject(
    id="meridian", name="Meridian Logistics Holdings", type=SubjectType.ENTITY, jurisdiction="SG"
)


@pytest.fixture()
def store() -> LocalDocumentStoreAdapter:
    return LocalDocumentStoreAdapter(
        replace(Settings.load(), profile="live", local=LocalSettings(documents_path=":memory:"))
    )


@pytest.fixture()
def service(store: LocalDocumentStoreAdapter):
    # An EMPTY knowledge base: whatever grounds this dossier came from the upload, not
    # from a seeded fixture corpus.
    return CddService(
        extraction=FakeExtraction(),
        knowledge_base=FakeKnowledgeBase(passages=[]),
        adverse_media=FakeAdverseMedia(),
        registry=FakeRegistry(),
        compliance=FakeCompliance(),
        llm=FakeLLM(),
        guardrail=FakeGuardrail(),
        redaction=FakeRedaction(),
        tracer=FakeTracer(),
        audit=FakeAudit(),
        document_store=store,
    )


def _upload(store: LocalDocumentStoreAdapter, content: bytes | None = None):
    return store.put(
        content=content if content is not None else build_pdf(BANK_STATEMENT_PAGES),
        filename="statement.pdf",
        doc_type=DocType.BANK_STATEMENT,
        subject_id=_SUBJECT.id,
        acl_tags=(f"case:{_SUBJECT.id}", f"tenant:{_TENANT}"),
        mime_type="application/pdf",
    )


def _assess(service: CddService, record):
    return service.assess(
        CaseInput(subject=_SUBJECT, documents=(record.to_kyc_document(),)),
        actor="demo.analyst@bank.example",
        principals=_PRINCIPALS,
        tenant=_TENANT,
    )


def test_the_uploaded_document_is_what_grounds_the_dossier(service, store):
    record = _upload(store)

    case = _assess(service, record)

    cited = {c.source_id for c in case.sow.citations}
    assert cited == {record.id}, "the dossier must cite the uploaded document, nothing else"


def test_citations_name_the_page_and_link_back_to_the_bytes(service, store):
    record = _upload(store)

    case = _assess(service, record)

    for citation in case.sow.citations:
        assert citation.page in (1, 2), "every citation names a real page of the document"
        assert citation.url == f"/v1/cases/meridian/documents/{record.id}"


def test_the_citation_link_comes_from_custody_not_the_request(service, store):
    """A request body cannot decide where "source" sends a reviewer."""
    record = _upload(store)
    spoofed = replace(record.to_kyc_document(), uri="https://attacker.example/phishing-page")

    case = service.assess(
        CaseInput(subject=_SUBJECT, documents=(spoofed,)),
        actor="demo.analyst@bank.example",
        principals=_PRINCIPALS,
        tenant=_TENANT,
    )

    urls = {c.url for c in case.sow.citations}
    assert urls == {f"/v1/cases/meridian/documents/{record.id}"}


@pytest.mark.parametrize(
    ("phrase", "expected_page"),
    [("Opening balance statement of account", 1), ("Orchard Rise property sale proceeds", 2)],
)
def test_retrieval_attributes_a_phrase_to_the_page_it_is_printed_on(
    service, store, phrase: str, expected_page: int
):
    record = _upload(store)
    _assess(service, record)

    passages = service._knowledge_base.search(  # noqa: SLF001 - asserting on the index itself
        RetrievalQuery(
            text=phrase,
            acl_principals=(f"case:{_SUBJECT.id}", f"tenant:{_TENANT}", *_PRINCIPALS),
            top_k=1,
        )
    )

    assert [p.citation.page for p in passages] == [expected_page]


def test_the_page_count_is_written_back_to_the_document_record(service, store):
    record = _upload(store)

    _assess(service, record)

    assert store.metadata(record.id, ("case:meridian", f"tenant:{_TENANT}")).pages == 2


def test_a_document_belonging_to_another_tenant_grounds_nothing(store):
    """The pipeline reads bytes under the CALLER's principals, not the case tag alone."""
    foreign = store.put(
        content=build_pdf(BANK_STATEMENT_PAGES),
        filename="stolen.pdf",
        doc_type=DocType.BANK_STATEMENT,
        subject_id=_SUBJECT.id,
        acl_tags=(f"case:{_SUBJECT.id}", "tenant:other-bank"),
        mime_type="application/pdf",
    )
    service = CddService(
        extraction=FakeExtraction(),
        knowledge_base=FakeKnowledgeBase(passages=[]),
        adverse_media=FakeAdverseMedia(),
        registry=FakeRegistry(),
        compliance=FakeCompliance(),
        llm=FakeLLM(),
        guardrail=FakeGuardrail(),
        redaction=FakeRedaction(),
        tracer=FakeTracer(),
        audit=FakeAudit(),
        document_store=store,
    )

    # Nothing readable was ingested, so the case is ungrounded and refused outright
    # rather than assembled from another tenant's evidence.
    with pytest.raises(RetrievalEmptyError):
        _assess(service, foreign)


def test_a_case_with_no_document_store_still_works(store):
    """The CLI and the offline tests pass no store; the dossier grounds on the index."""
    service = CddService(
        extraction=FakeExtraction(),
        knowledge_base=FakeKnowledgeBase(),  # the seeded fixture corpus
        adverse_media=FakeAdverseMedia(),
        registry=FakeRegistry(),
        compliance=FakeCompliance(),
        llm=FakeLLM(),
        guardrail=FakeGuardrail(),
        redaction=FakeRedaction(),
        tracer=FakeTracer(),
        audit=FakeAudit(),
    )

    case = _assess(service, _upload(store))

    assert case.sow.citations
