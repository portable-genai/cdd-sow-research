"""GCP document-store adapter (DocumentStorePort) — regional CMEK object custody.

Holds every uploaded case document as an object in a regional, CMEK-encrypted Cloud
Storage bucket (``settings.document_store.bucket``) inside the VPC-SC perimeter, so
customer evidence stays in-region and inherits the deployment's key management and
audit posture. Metadata (case, ACL tags, digest, page count) rides on the object's own
custom metadata, so a document and its access-control facts cannot drift apart.

Access control mirrors the knowledge base exactly: subset match, fail-closed, and the
same error for "absent" and "not readable" so ids cannot be probed. Bucket-level IAM is
the outer boundary; this per-object check is the object-level one.

The Cloud Storage SDK import is lazy so the local/on-prem/test profiles import without it.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from ...config import Settings
from ...domain.errors import DocumentConflictError, DocumentNotFoundError
from ...domain.models import DocType, StoredDocument, document_id

_ACL_SEP = "|"  # object metadata values are plain strings


class GcsDocumentStoreAdapter:
    """Store and serve uploaded case documents from a regional CMEK bucket."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._bucket_name = settings.document_store.bucket
        self._prefix = settings.document_store.prefix
        self._client: Any | None = None

    # ------------------------------------------------------------------ #
    # Client
    # ------------------------------------------------------------------ #
    def _bucket(self) -> Any:
        from google.cloud import storage  # lazy import (GCP SDK only on this path)

        if self._client is None:
            self._client = storage.Client(project=self._settings.project_id)
        return self._client.bucket(self._bucket_name)

    def _blob(self, document_id: str) -> Any:
        return self._bucket().blob(f"{self._prefix}{document_id}")

    # ------------------------------------------------------------------ #
    # DocumentStorePort
    # ------------------------------------------------------------------ #
    def put(
        self,
        content: bytes,
        filename: str,
        doc_type: DocType,
        subject_id: str,
        acl_tags: tuple[str, ...],
        mime_type: str = "",
    ) -> StoredDocument:
        record = StoredDocument(
            id=document_id(content, subject_id, doc_type, filename),
            filename=filename,
            doc_type=doc_type,
            mime_type=mime_type or "application/octet-stream",
            size_bytes=len(content),
            pages=0,
            subject_id=subject_id,
            acl_tags=tuple(acl_tags),
            uploaded_at=datetime.now(UTC).isoformat(timespec="seconds"),
            sha256=hashlib.sha256(content).hexdigest(),
        )
        blob = self._blob(record.id)
        blob.metadata = {
            "filename": record.filename,
            "doc_type": record.doc_type.value,
            "subject_id": record.subject_id,
            "acl_tags": _ACL_SEP.join(record.acl_tags),
            "uploaded_at": record.uploaded_at,
            "sha256": record.sha256,
            "pages": "0",
        }
        # CMEK: the bucket's default KMS key encrypts the object; no per-call key here.
        blob.upload_from_string(content, content_type=record.mime_type)
        return record

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
        sha256 = hashlib.sha256(content).hexdigest()
        blob = self._blob(document_id)
        if blob.exists():
            blob.reload()
            # Same bytes already in the bucket: a repeated reload, not a collision.
            if (blob.metadata or {}).get("sha256", "") == sha256:
                return self._to_record(document_id, blob)
            raise DocumentConflictError(
                f"document id {document_id!r} is already held with different content"
            )
        record = StoredDocument(
            id=document_id,
            filename=filename,
            doc_type=doc_type,
            mime_type=mime_type or "application/octet-stream",
            size_bytes=len(content),
            pages=int(pages),
            subject_id=subject_id,
            acl_tags=tuple(acl_tags),
            uploaded_at=uploaded_at or datetime.now(UTC).isoformat(timespec="seconds"),
            sha256=sha256,
        )
        blob.metadata = {
            "filename": record.filename,
            "doc_type": record.doc_type.value,
            "subject_id": record.subject_id,
            "acl_tags": _ACL_SEP.join(record.acl_tags),
            "uploaded_at": record.uploaded_at,
            "sha256": record.sha256,
            "pages": str(record.pages),
        }
        # CMEK: the bucket's default KMS key encrypts the object, exactly as for an
        # upload. A restored document is customer evidence and gets the same posture.
        blob.upload_from_string(content, content_type=record.mime_type)
        return record

    def get(self, document_id: str, acl_principals: tuple[str, ...]) -> bytes:
        blob = self._blob(document_id)
        if not blob.exists():
            raise DocumentNotFoundError(f"no readable document {document_id!r}")
        blob.reload()
        self._require_readable(blob.metadata, document_id, acl_principals)
        content: bytes = blob.download_as_bytes()
        return content

    def metadata(self, document_id: str, acl_principals: tuple[str, ...]) -> StoredDocument:
        blob = self._blob(document_id)
        if not blob.exists():
            raise DocumentNotFoundError(f"no readable document {document_id!r}")
        blob.reload()
        self._require_readable(blob.metadata, document_id, acl_principals)
        return self._to_record(document_id, blob)

    def list_documents(
        self, acl_principals: tuple[str, ...], subject_id: str = ""
    ) -> list[StoredDocument]:
        blobs = list(self._bucket().list_blobs(prefix=self._prefix))
        out: list[StoredDocument] = []
        for blob in blobs:
            meta = blob.metadata or {}
            if subject_id and meta.get("subject_id", "") != subject_id:
                continue
            if not self._acl_ok(self._split_tags(meta.get("acl_tags")), acl_principals):
                continue
            out.append(self._to_record(blob.name.removeprefix(self._prefix), blob))
        out.sort(key=lambda r: r.uploaded_at, reverse=True)
        return out

    def delete(self, document_id: str, acl_principals: tuple[str, ...]) -> bool:
        blob = self._blob(document_id)
        if not blob.exists():
            return False
        blob.reload()
        self._require_readable(blob.metadata, document_id, acl_principals)
        blob.delete()
        return True

    def set_pages(self, document_id: str, pages: int) -> None:
        blob = self._blob(document_id)
        if not blob.exists():
            return
        blob.reload()
        blob.metadata = {**(blob.metadata or {}), "pages": str(int(pages))}
        blob.patch()

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @classmethod
    def _require_readable(
        cls, meta: dict[str, str] | None, document_id: str, acl_principals: tuple[str, ...]
    ) -> None:
        if not cls._acl_ok(cls._split_tags((meta or {}).get("acl_tags")), acl_principals):
            raise DocumentNotFoundError(f"no readable document {document_id!r}")

    @staticmethod
    def _acl_ok(document_tags: tuple[str, ...], acl_principals: tuple[str, ...]) -> bool:
        if not document_tags:
            return True
        return set(document_tags) <= set(acl_principals)

    @staticmethod
    def _split_tags(raw: str | None) -> tuple[str, ...]:
        return tuple(t for t in (raw or "").split(_ACL_SEP) if t)

    @classmethod
    def _to_record(cls, document_id: str, blob: Any) -> StoredDocument:
        meta = blob.metadata or {}
        try:
            doc_type = DocType(meta.get("doc_type", "other"))
        except ValueError:
            doc_type = DocType.OTHER
        return StoredDocument(
            id=document_id,
            filename=meta.get("filename", ""),
            doc_type=doc_type,
            mime_type=blob.content_type or "application/octet-stream",
            size_bytes=int(blob.size or 0),
            pages=int(meta.get("pages", "0") or 0),
            subject_id=meta.get("subject_id", ""),
            acl_tags=cls._split_tags(meta.get("acl_tags")),
            uploaded_at=meta.get("uploaded_at", ""),
            sha256=meta.get("sha256", ""),
        )
