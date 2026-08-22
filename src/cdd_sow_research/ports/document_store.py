"""DocumentStorePort — durable custody of the case documents a user uploads.

The dossier is only as traceable as its evidence: a citation that reads "doc-bank p.11"
is worthless unless a reviewer can open page 11 of that exact file. This port owns the
bytes behind every uploaded KYC document, so the pipeline can extract them and the API
can serve them back under the same access control that governs retrieval.

Primary GCP adapter: an object-store bucket (regional, CMEK). The ``local`` profile keeps
the bytes in a SQLite blob store on disk; on-prem swaps in the client's own case vault
with no change to callers.

Access control is the knowledge base's contract, repeated here on purpose: a document is
visible only to a caller holding EVERY one of its ``acl_tags`` (subset match,
fail-closed), so a ``case:<id>`` principal alone never crosses a tenant boundary. A
document the caller may not read and a document that does not exist raise the same
:class:`~cdd_sow_research.domain.errors.DocumentNotFoundError`, so probing ids leaks nothing.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..domain.models import DocType, StoredDocument


@runtime_checkable
class DocumentStorePort(Protocol):
    def put(
        self,
        content: bytes,
        filename: str,
        doc_type: DocType,
        subject_id: str,
        acl_tags: tuple[str, ...],
        mime_type: str = "",
    ) -> StoredDocument:
        """Store ``content`` and return its record (id, digest, size) for the case."""
        ...

    def get(self, document_id: str, acl_principals: tuple[str, ...]) -> bytes:
        """Return the stored bytes, or raise ``DocumentNotFoundError`` if not readable."""
        ...

    def metadata(self, document_id: str, acl_principals: tuple[str, ...]) -> StoredDocument:
        """Return the record without the bytes (same fail-closed ACL as ``get``)."""
        ...

    def list_documents(
        self, acl_principals: tuple[str, ...], subject_id: str = ""
    ) -> list[StoredDocument]:
        """List readable documents, newest first, optionally filtered to one subject."""
        ...

    def delete(self, document_id: str, acl_principals: tuple[str, ...]) -> bool:
        """Delete a readable document; False when it was already absent."""
        ...

    def set_pages(self, document_id: str, pages: int) -> None:
        """Record the page count discovered during extraction (best-effort metadata)."""
        ...

    def restore(
        self,
        content: bytes,
        document_id: str,
        filename: str,
        doc_type: DocType,
        subject_id: str,
        acl_tags: tuple[str, ...],
        mime_type: str = "",
        pages: int = 0,
        uploaded_at: str = "",
    ) -> StoredDocument:
        """Place a document back in custody under its ORIGINAL id (bundle reload, P-12).

        Separate from :meth:`put` because the id is the whole point. ``put`` mints a new
        id for a fresh upload; ``restore`` keeps the one the dossier's citations already
        name, so a bundle that lands in a different deployment still resolves "doc-bank
        p.11" to the same file. ``uploaded_at`` is likewise carried over: the custody
        record should say when the bank received the evidence, not when it was copied.

        ``acl_tags`` is derived by the RESTORING side from its own verified principal and
        passed in here; the adapter stores what it is given and never reads tags out of
        the bundle.

        Raises :class:`~cdd_sow_research.domain.errors.DocumentConflictError` when
        ``document_id`` is already held with different bytes. Restoring the same bytes
        onto the same id is idempotent and returns the existing record, so re-running a
        reload after a partial failure is safe.
        """
        ...
