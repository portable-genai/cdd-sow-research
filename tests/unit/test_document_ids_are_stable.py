"""Whether uploading the same evidence twice makes one document or two.

``KycDocument.id`` is documented as "stable doc id within the case". It was
``uuid4().hex[:12]``, minted fresh on every upload, so the documentation described a
property the code did not have. Offering the same document twice produced two documents,
and nothing downstream deduplicated them.

The consequence lands in the dossier rather than in the store. Retrieval ranks passages and
cites what it retrieves, so N copies of one bank statement are cited as N independent
sources corroborating the same claim -- which is precisely the reading a citation count
exists to support and, here, to mislead. The managed store had accumulated eight copies of
a single synthetic statement, one per demo run, and the source-of-wealth narrative was
grounded in "eight documents".

What counts as the same document is the whole filing: subject, kind, filename and bytes.
Bytes alone would merge two deliberate filings of one PDF; bytes plus subject alone still
would. Anything narrower than the subject would merge across cases and let one case's
access decision reach another's evidence.
"""

from __future__ import annotations

from cdd_sow_research.adapters.local.document_store import LocalDocumentStoreAdapter
from cdd_sow_research.config import LocalSettings, Settings
from cdd_sow_research.domain.models import DocType, document_id

_BYTES = b"%PDF-1.7\nMERIDIAN HARBOUR HOLDINGS - statement\n"


def _store() -> LocalDocumentStoreAdapter:
    return LocalDocumentStoreAdapter(
        Settings(
            profile="local",
            local=LocalSettings(
                db_path=":memory:", audit_path=":memory:", documents_path=":memory:"
            ),
        )
    )


def _put(store: LocalDocumentStoreAdapter, **overrides: object):  # type: ignore[no-untyped-def]
    kwargs: dict[str, object] = {
        "content": _BYTES,
        "filename": "statement.pdf",
        "doc_type": DocType.BANK_STATEMENT,
        "subject_id": "subj-meridian",
        "acl_tags": ("case:meridian",),
        "mime_type": "application/pdf",
    }
    kwargs.update(overrides)
    return store.put(**kwargs)  # type: ignore[arg-type]


def test_the_same_filing_uploaded_twice_is_one_document() -> None:
    """The defect: this produced two ids, and both were indexed and cited."""

    store = _store()

    first = _put(store)
    second = _put(store)

    assert first.id == second.id
    assert len(store.list_documents(("case:meridian",), subject_id="subj-meridian")) == 1


def test_two_deliberate_filings_of_the_same_bytes_stay_two_documents() -> None:
    """Idempotence must not become merging. A statement filed also as a registry extract
    is two filings, and collapsing them would silently discard one."""

    store = _store()

    statement = _put(store)
    extract = _put(store, doc_type=DocType.REGISTRY_EXTRACT, filename="registry.pdf")

    assert statement.id != extract.id
    assert len(store.list_documents(("case:meridian",), subject_id="subj-meridian")) == 2


def test_identical_bytes_under_two_subjects_are_two_documents() -> None:
    """The security half. Merging across subjects would give one case's access decision
    authority over another case's evidence."""

    assert document_id(_BYTES, "subj-a", DocType.BANK_STATEMENT, "s.pdf") != document_id(
        _BYTES, "subj-b", DocType.BANK_STATEMENT, "s.pdf"
    )


def test_re_uploading_does_not_widen_who_can_read_the_document() -> None:
    """A repeat keeps the ACL the store already applied.

    Otherwise re-upload becomes an access-control mechanism: anyone able to POST the same
    bytes could re-tag evidence into their own case. The store returns what it HOLDS, so
    the caller is told the tags in force rather than the ones it proposed.
    """

    store = _store()
    _put(store)

    repeat = _put(store, acl_tags=("case:someone-else",))

    assert repeat.acl_tags == ("case:meridian",)
    assert store.list_documents(("case:someone-else",), subject_id="subj-meridian") == []


def test_different_content_under_one_filing_is_a_different_document() -> None:
    """A corrected re-upload is new evidence, not a repeat, and must not be dropped."""

    store = _store()

    first = _put(store)
    revised = _put(store, content=_BYTES + b"revised\n")

    assert first.id != revised.id
    assert len(store.list_documents(("case:meridian",), subject_id="subj-meridian")) == 2
