"""The repair decision, exercised on the shapes that actually occurred.

The deployment held 22 documents for one case where the filing derives exactly one id. The
stable-id fix stops new duplicates and cannot retract stored ones, so the corpus stayed
corrupted and the paired demonstration's citation counts diverged on history rather than on any
disagreement between the profiles.

Every case below is a shape the repair must get right in production, and the two that matter most
are the refusals: a filing whose canonical copy is absent keeps everything, and two filings that
merely share a filename are never merged.
"""

from __future__ import annotations

from cdd_sow_research.domain.corpus_repair import plan_repair, stale_ids
from cdd_sow_research.domain.models import DocType, StoredDocument, document_id

_SUBJECT = "meridian-harbour-holdings-pte-ltd"
_CONTENT = b"MERIDIAN HARBOUR HOLDINGS PTE LTD\nsource of wealth statement\n"
_NAME = "synthetic-evidence.txt"


def _canonical(content: bytes = _CONTENT, filename: str = _NAME) -> str:
    return document_id(content, _SUBJECT, DocType.BANK_STATEMENT, filename)


def _doc(doc_id: str, *, subject: str = _SUBJECT, filename: str = _NAME) -> StoredDocument:
    return StoredDocument(
        id=doc_id,
        filename=filename,
        doc_type=DocType.BANK_STATEMENT,
        subject_id=subject,
        acl_tags=(f"case:{subject}", "tenant:reference-bank"),
    )


def test_the_accumulated_copies_are_stale_and_the_derived_one_is_kept() -> None:
    canonical = _canonical()
    minted = ["doc-07e499c3c8a8", "doc-11f6091037fd", "doc-1ea63d10fa2d"]
    documents = [_doc(canonical)] + [_doc(i) for i in minted]
    contents = {d.id: _CONTENT for d in documents}

    groups = plan_repair(documents, contents)

    assert len(groups) == 1
    assert groups[0].canonical_id == canonical
    assert groups[0].canonical_present is True
    assert set(groups[0].stale_ids) == set(minted)
    assert canonical not in stale_ids(groups)


def test_a_filing_whose_canonical_copy_is_absent_keeps_every_copy() -> None:
    """Deleting the only evidence a case has is worse than citing it twice."""
    documents = [_doc("doc-minted-a"), _doc("doc-minted-b")]
    contents = {d.id: _CONTENT for d in documents}

    groups = plan_repair(documents, contents)

    assert groups[0].canonical_present is False
    assert groups[0].stale_ids == ()
    assert stale_ids(groups) == ()


def test_two_filings_sharing_a_filename_are_not_merged() -> None:
    """Different bytes under one name are two filings, and merging them discards one."""
    other = b"a different statement entirely\n"
    first, second = _canonical(), _canonical(other)
    documents = [_doc(first), _doc(second), _doc("doc-stale-copy")]
    contents = {first: _CONTENT, second: other, "doc-stale-copy": _CONTENT}

    groups = plan_repair(documents, contents)

    assert len(groups) == 2
    assert {g.canonical_id for g in groups} == {first, second}
    # The stale copy belongs to the filing whose bytes it carries, and only that one.
    assert stale_ids(groups) == ("doc-stale-copy",)


def test_identical_bytes_filed_under_two_subjects_stay_two_documents() -> None:
    """Idempotence must not become SHARING: collapsing these crosses an ACL boundary."""
    mine = _doc(_canonical())
    theirs = _doc(
        document_id(_CONTENT, "another-subject", DocType.BANK_STATEMENT, _NAME),
        subject="another-subject",
    )
    contents = {mine.id: _CONTENT, theirs.id: _CONTENT}

    groups = plan_repair([mine, theirs], contents)

    assert len(groups) == 2
    assert stale_ids(groups) == ()


def test_a_document_whose_bytes_cannot_be_read_is_never_deleted() -> None:
    """Without the content there is no derivable id, and a repair must not delete on a hunch."""
    canonical = _canonical()
    documents = [_doc(canonical), _doc("doc-unreadable")]

    groups = plan_repair(documents, {canonical: _CONTENT})

    assert stale_ids(groups) == ()
