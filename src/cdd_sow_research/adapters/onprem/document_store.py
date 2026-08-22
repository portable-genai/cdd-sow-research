"""On-prem placeholder for ``DocumentStorePort`` — the sovereign target.

A reversibility (P-02, P-12) migration placeholder: in the managed profile this port
binds to a regional, CMEK-encrypted object bucket inside the VPC-SC perimeter; switching
``profile`` to ``onprem`` rebinds it here, to the client's own case vault. The adapter
constructs cleanly with **no external dependencies** and structurally satisfies the same
Protocol as the managed adapter, so the contract tests prove interface parity.

Every method raises rather than degrading: a document store that silently drops customer
evidence, or silently serves a document to a caller who may not read it, is worse than
one that refuses. Filling these bodies in against the client's vault is the only change
required; nothing in the domain or the API changes.
"""

from __future__ import annotations

from ...config import Settings
from ...domain.models import DocType, StoredDocument

_MESSAGE = (
    "On-prem DocumentStorePort adapter is a migration placeholder; implement against your "
    "on-premise case document vault (durable, encrypted, ACL-scoped). Core domain logic is "
    "unchanged."
)


class OnPremDocumentStoreAdapter:
    """Placeholder document-custody adapter for the on-prem profile."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def put(
        self,
        content: bytes,
        filename: str,
        doc_type: DocType,
        subject_id: str,
        acl_tags: tuple[str, ...],
        mime_type: str = "",
    ) -> StoredDocument:
        raise NotImplementedError(_MESSAGE)

    def get(self, document_id: str, acl_principals: tuple[str, ...]) -> bytes:
        raise NotImplementedError(_MESSAGE)

    def metadata(self, document_id: str, acl_principals: tuple[str, ...]) -> StoredDocument:
        raise NotImplementedError(_MESSAGE)

    def list_documents(
        self, acl_principals: tuple[str, ...], subject_id: str = ""
    ) -> list[StoredDocument]:
        raise NotImplementedError(_MESSAGE)

    def delete(self, document_id: str, acl_principals: tuple[str, ...]) -> bool:
        raise NotImplementedError(_MESSAGE)

    def set_pages(self, document_id: str, pages: int) -> None:
        raise NotImplementedError(_MESSAGE)

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
        raise NotImplementedError(_MESSAGE)
