"""Local document-store adapter (DocumentStorePort) — SQLite blob custody.

The ``local`` profile's stand-in for a regional CMEK object bucket: uploaded case
documents are held as BLOBs in a ``sqlite3`` database alongside their metadata and ACL
tags, so a laptop run keeps the same custody + access-control behaviour as the managed
adapter with no cloud account. The bytes never leave the machine.

Access control is subset-match and fail-closed (identical to the local knowledge base):
a document is readable only by a caller holding every one of its ACL tags. A document
that is absent and one the caller may not read raise the same error, so a caller cannot
probe for the existence of another tenant's evidence.

Default path is ``~/.cdd_sow_research/documents.db`` (override with ``CDD_LOCAL_DOCUMENTS``
or ``local.documents_path``); tests pass ``:memory:``.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

from ...config import Settings
from ...domain.errors import DocumentConflictError, DocumentNotFoundError
from ...domain.models import DocType, StoredDocument, document_id

_DEFAULT_DB_PATH = Path.home() / ".cdd_sow_research" / "documents.db"
_ACL_SEP = "␟"  # unit separator: joins acl_tags into one column safely


class LocalDocumentStoreAdapter:
    """Store and serve uploaded case documents from a local SQLite blob store."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        db_path = getattr(getattr(settings, "local", None), "documents_path", "") or str(
            _DEFAULT_DB_PATH
        )
        self._db_path = db_path
        # Same threading contract as the local knowledge base: one process-wide adapter
        # is shared by Starlette's worker threadpool, so the connection is opened with
        # check_same_thread=False and every access is serialised by an RLock.
        self._lock = threading.RLock()
        self._conn = self._connect(db_path)
        self._init_schema()

    # ------------------------------------------------------------------ #
    # Connection / schema
    # ------------------------------------------------------------------ #
    @staticmethod
    def _connect(db_path: str) -> sqlite3.Connection:
        if db_path not in (":memory:", "") and not db_path.startswith("file:"):
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    doc_type TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    pages INTEGER NOT NULL DEFAULT 0,
                    subject_id TEXT NOT NULL DEFAULT '',
                    acl_tags TEXT NOT NULL DEFAULT '',
                    uploaded_at TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    content BLOB NOT NULL
                )
                """
            )
            self._conn.commit()

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
        with self._lock:
            # The id is derived from the filing, so offering the same document twice is a
            # REPEAT rather than a second document. Ignoring the second insert keeps the
            # first record's ACL tags and upload time, which is the conservative choice:
            # re-uploading evidence must never be a way to widen who can read it.
            self._conn.execute(
                "INSERT OR IGNORE INTO documents (id, filename, doc_type, mime_type, "
                "size_bytes, pages, subject_id, acl_tags, uploaded_at, sha256, content) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.id,
                    record.filename,
                    record.doc_type.value,
                    record.mime_type,
                    record.size_bytes,
                    record.pages,
                    record.subject_id,
                    _ACL_SEP.join(record.acl_tags),
                    record.uploaded_at,
                    record.sha256,
                    content,
                ),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM documents WHERE id = ?", (record.id,)
            ).fetchone()
        # Return what the store HOLDS, not what this call proposed: on a repeat those
        # differ (upload time, and the ACL tags deliberately not widened), and returning
        # the proposal would report an ACL the store never applied.
        return self._row_to_record(row) if row is not None else record

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
        with self._lock:
            existing = self._conn.execute(
                "SELECT id, filename, doc_type, mime_type, size_bytes, pages, subject_id, "
                "acl_tags, uploaded_at, sha256 FROM documents WHERE id = ?",
                (document_id,),
            ).fetchone()
            if existing is not None:
                # Same bytes already in custody: a repeated reload, not a collision. The
                # digest is the whole test, so a differing document under the same id is
                # refused even when the caller could otherwise read it.
                if existing["sha256"] == sha256:
                    return self._row_to_record(existing)
                raise DocumentConflictError(
                    f"document id {document_id!r} is already held with different content"
                )
            self._conn.execute(
                "INSERT INTO documents (id, filename, doc_type, mime_type, size_bytes, "
                "pages, subject_id, acl_tags, uploaded_at, sha256, content) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.id,
                    record.filename,
                    record.doc_type.value,
                    record.mime_type,
                    record.size_bytes,
                    record.pages,
                    record.subject_id,
                    _ACL_SEP.join(record.acl_tags),
                    record.uploaded_at,
                    record.sha256,
                    content,
                ),
            )
            self._conn.commit()
        return record

    def get(self, document_id: str, acl_principals: tuple[str, ...]) -> bytes:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM documents WHERE id = ?", (document_id,)
            ).fetchone()
        self._require_readable(row, document_id, acl_principals)
        assert row is not None  # narrowed by _require_readable
        return bytes(row["content"])

    def metadata(self, document_id: str, acl_principals: tuple[str, ...]) -> StoredDocument:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, filename, doc_type, mime_type, size_bytes, pages, subject_id, "
                "acl_tags, uploaded_at, sha256 FROM documents WHERE id = ?",
                (document_id,),
            ).fetchone()
        self._require_readable(row, document_id, acl_principals)
        assert row is not None  # narrowed by _require_readable
        return self._row_to_record(row)

    def list_documents(
        self, acl_principals: tuple[str, ...], subject_id: str = ""
    ) -> list[StoredDocument]:
        sql = (
            "SELECT id, filename, doc_type, mime_type, size_bytes, pages, subject_id, "
            "acl_tags, uploaded_at, sha256 FROM documents"
        )
        params: list[object] = []
        if subject_id:
            sql += " WHERE subject_id = ?"
            params.append(subject_id)
        sql += " ORDER BY uploaded_at DESC, rowid DESC"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [
            record
            for record in (self._row_to_record(r) for r in rows)
            if self._acl_ok(record.acl_tags, acl_principals)
        ]

    def delete(self, document_id: str, acl_principals: tuple[str, ...]) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT acl_tags FROM documents WHERE id = ?", (document_id,)
            ).fetchone()
            if row is None:
                return False
            if not self._acl_ok(self._split_tags(row["acl_tags"]), acl_principals):
                raise DocumentNotFoundError(f"no readable document {document_id!r}")
            self._conn.execute("DELETE FROM documents WHERE id = ?", (document_id,))
            self._conn.commit()
        return True

    def set_pages(self, document_id: str, pages: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE documents SET pages = ? WHERE id = ?", (int(pages), document_id)
            )
            self._conn.commit()

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @classmethod
    def _require_readable(
        cls, row: sqlite3.Row | None, document_id: str, acl_principals: tuple[str, ...]
    ) -> None:
        """Raise the SAME error for absent and unreadable, so ids cannot be probed."""
        if row is None or not cls._acl_ok(cls._split_tags(row["acl_tags"]), acl_principals):
            raise DocumentNotFoundError(f"no readable document {document_id!r}")

    @staticmethod
    def _acl_ok(document_tags: tuple[str, ...], acl_principals: tuple[str, ...]) -> bool:
        """Subset, fail-closed: the caller must hold every one of the document's tags."""
        if not document_tags:
            return True
        return set(document_tags) <= set(acl_principals)

    @staticmethod
    def _split_tags(raw: str | None) -> tuple[str, ...]:
        return tuple(t for t in (raw or "").split(_ACL_SEP) if t)

    @classmethod
    def _row_to_record(cls, row: sqlite3.Row) -> StoredDocument:
        try:
            doc_type = DocType(row["doc_type"])
        except ValueError:
            doc_type = DocType.OTHER
        return StoredDocument(
            id=row["id"],
            filename=row["filename"],
            doc_type=doc_type,
            mime_type=row["mime_type"],
            size_bytes=int(row["size_bytes"]),
            pages=int(row["pages"]),
            subject_id=row["subject_id"],
            acl_tags=cls._split_tags(row["acl_tags"]),
            uploaded_at=row["uploaded_at"],
            sha256=row["sha256"],
        )
