"""Decide which of a case's stored documents are pre-fix duplicates of one filing.

``document_id`` derives a document's id from its FILING -- subject, doc type, filename and
content -- so uploading the same evidence twice overwrites rather than accumulates. It did not
always: ids were a fresh ``uuid4`` per upload until 2026-08-25, so a case's corpus grew by one
copy per run and retrieval cited the same page as several independent sources, which is the one
failure mode a citation count exists to rule out.

Fixing the derivation stops new duplicates. It cannot retract the ones already stored, and on the
named deployment there were 21 of them for a single synthetic bank statement -- enough that the
paired demonstration's citation counts diverged on pure history rather than on any disagreement
between the profiles. A code fix that leaves the corrupted state in place is half a fix.

**This module decides; it does not delete.** The decision is pure and testable, and the caller
performs the removal through the store's own ACL-checked ``delete``, so a repair cannot reach a
document the caller could not already read. That split is deliberate: the dangerous half is the
one worth keeping trivial to inspect.

The rule is conservative in the direction that matters. A document is stale only when the store
holds another document for the SAME filing whose id is the one ``document_id`` derives from that
filing -- the canonical copy must be present before anything is removed. If no canonical copy
exists, every copy is kept and reported, because deleting the only evidence a case has is worse
than citing it twice.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .models import DocType, StoredDocument, document_id


@dataclass(frozen=True, slots=True)
class FilingGroup:
    """One filing, and every stored document that claims to be it."""

    subject_id: str
    doc_type: DocType
    filename: str
    canonical_id: str
    #: Present when the store holds the canonical copy. When it does not, nothing is removed.
    canonical_present: bool
    stale_ids: tuple[str, ...]


def plan_repair(
    documents: Sequence[StoredDocument],
    contents: dict[str, bytes],
) -> tuple[FilingGroup, ...]:
    """Group ``documents`` by filing and name the stale copies in each.

    ``contents`` maps a document id to its bytes. A document whose bytes the caller could not
    read is skipped entirely rather than guessed at: without the content there is no derivable
    id, and a repair that assumes one would delete on a hunch.
    """
    groups: dict[tuple[str, str, str], list[StoredDocument]] = {}
    for record in documents:
        if record.id not in contents:
            continue
        groups.setdefault((record.subject_id, record.doc_type.value, record.filename), []).append(
            record
        )

    out: list[FilingGroup] = []
    for (subject_id, doc_type_value, filename), members in sorted(groups.items()):
        # The canonical id is a property of the whole filing, so members agree on it only when
        # they agree on the content too. Differing bytes under one filename are a DIFFERENT
        # filing, and re-splitting here is what stops a repair merging two of them.
        by_canonical: dict[str, list[StoredDocument]] = {}
        for record in members:
            canonical = document_id(
                contents[record.id], record.subject_id, record.doc_type, record.filename
            )
            by_canonical.setdefault(canonical, []).append(record)
        for canonical, same_filing in sorted(by_canonical.items()):
            present = any(record.id == canonical for record in same_filing)
            stale = tuple(sorted(r.id for r in same_filing if r.id != canonical))
            out.append(
                FilingGroup(
                    subject_id=subject_id,
                    doc_type=DocType(doc_type_value),
                    filename=filename,
                    canonical_id=canonical,
                    canonical_present=present,
                    stale_ids=stale if present else (),
                )
            )
    return tuple(out)


def stale_ids(groups: Iterable[FilingGroup]) -> tuple[str, ...]:
    """Every id a repair would remove, across all filings."""
    return tuple(doc_id for group in groups for doc_id in group.stale_ids)
