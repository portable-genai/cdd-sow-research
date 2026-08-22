"""Local audit adapter (AuditSinkPort) - append-only, hash-chained local WORM stand-in.

The ``local`` profile's stand-in for the **Cloud Logging locked WORM bucket**: an
append-only, hash-chained store that records already-redacted audit events and supports
read-back, tamper verification, and an open-format JSON Lines export/restore.

**Sourced from the shared ``hex-service-kit`` commons.** The hash-chain
engine (canonical encoding, ``entry_hash = SHA-256(prev_hash || event_json)``, WORM triggers,
tamper verification, the external head anchor, and the JSONL export/restore) lives in
:class:`hex_service_kit.audit.HashChainedAuditLog`, which this adapter delegates to rather than
copying. This adapter keeps this repo's ``Settings``-driven path resolution, the
``CDD_LOCAL_AUDIT_ANCHOR`` env var, and its ``AuditEvent`` shape; ``AuditChainError`` and
``ChainReport`` are re-exported from the package so callers and tests import them from here.
Records serialise with the domain ``to_jsonable``-equivalent inside the package, so a stored
event round-trips through JSON exactly like the managed sink writes it, and
``audit_event_from_jsonable`` rehydrates an exported line back into a first-class ``AuditEvent``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from importlib import import_module
from pathlib import Path
from typing import Any

from hex_service_kit.audit import AuditChainError, ChainReport, HashChainedAuditLog

from ...config import Settings
from ...domain.models import AuditEvent
from ...envread import optional_setting

__all__ = ["AuditChainError", "ChainReport", "LocalAppendOnlyAuditAdapter"]

_DEFAULT_DB_DIR = Path.home() / ".cdd_sow_research"
_DEFAULT_AUDIT_PATH = _DEFAULT_DB_DIR / "audit.db"
_IDEMPOTENCY_METADATA_KEY = "_audit_event_id"
_PATH_LOCKS: dict[str, threading.RLock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


def _path_lock(path: str) -> threading.RLock:
    """Share one in-process lock across adapters pointing at the same file."""
    key = str(Path(path).expanduser().resolve()) if path != ":memory:" else path
    with _PATH_LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.RLock())


class LocalAppendOnlyAuditAdapter:
    """Append-only, hash-chained audit store: records already-redacted events.

    A thin wrapper over :class:`hex_service_kit.audit.HashChainedAuditLog`: it resolves the
    store path from ``Settings`` and the external anchor from ``CDD_LOCAL_AUDIT_ANCHOR``, then
    delegates every operation to the shared engine.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        path = getattr(getattr(settings, "local", None), "audit_path", "") or str(
            _DEFAULT_AUDIT_PATH
        )
        self._path = path
        self._writer_thread_lock = _path_lock(path)
        self._writer_lock_path = (
            Path(path).expanduser().resolve().with_name(Path(path).name + ".writer.lock")
            if path not in ("", ":memory:") and not path.startswith("file:")
            else None
        )
        anchor = optional_setting("CDD_LOCAL_AUDIT_ANCHOR") or ""
        with self._exclusive_writer():
            self._log = HashChainedAuditLog(path, anchor_path=anchor)
            self._initialize_idempotency_index()

    @property
    def _conn(self) -> Any:
        """The underlying SQLite connection (used by tamper-simulation tests)."""
        return self._log._conn

    # ------------------------------------------------------------------ #
    # AuditSinkPort
    # ------------------------------------------------------------------ #
    def record(self, event: AuditEvent) -> None:
        """Append one immutable, already-redacted audit record (no update / delete)."""
        with self._exclusive_writer():
            self._require_current_external_anchor()
            self._log.record(event)

    def record_once(self, event_id: str, event: AuditEvent) -> None:
        """Atomically append a stable event once across threads, processes, and restarts."""
        if not event_id or event.trace_id != event_id:
            raise ValueError("idempotent audit event must carry its stable ID as trace_id")
        idempotent_event = replace(
            event,
            metadata={**event.metadata, _IDEMPOTENCY_METADATA_KEY: event_id},
        )
        with self._exclusive_writer():
            if self._has_idempotency_marker(event_id):
                # ``HashChainedAuditLog.record`` commits SQLite before updating its
                # external head anchor. A process can therefore die with the event and
                # marker durable but the anchor stale. Redelivery must repair that final
                # acknowledgement step before treating the event as complete, but only
                # when the old anchor is a verified prefix so repair cannot erase
                # evidence of tail truncation.
                self._advance_external_anchor(event_id)
                return
            self._require_current_external_anchor()
            try:
                # The AFTER INSERT trigger writes the marker in the same SQLite
                # transaction as the hash-chain row. A process death after commit but
                # before caller acknowledgement therefore remains retry-safe.
                self._log.record(idempotent_event)
            except sqlite3.IntegrityError:
                self._conn.rollback()
                if self._has_idempotency_marker(event_id):
                    self._advance_external_anchor(event_id)
                    return
                raise

    def read_all(self) -> list[dict]:
        """Read back every stored event (oldest first) for inspection / assertions."""
        return self._log.read_all()

    # ------------------------------------------------------------------ #
    # Tamper evidence + open-format export / restore (P-08, P-12)
    # ------------------------------------------------------------------ #
    def verify_chain(self) -> ChainReport:
        """Re-derive every hash from the stored bytes and confirm the chain links."""
        return self._log.verify_chain()

    def export_jsonl(self, path: str | Path) -> int:
        """Export the trail to JSON Lines with the per-record chain hashes."""
        return self._log.export_jsonl(path)

    def import_jsonl(self, path: str | Path) -> int:
        """Restore an exported trail into this (empty) store, re-verifying every link."""
        with self._exclusive_writer():
            imported = self._log.import_jsonl(path)
            self._backfill_idempotency_index()
            return imported

    # ------------------------------------------------------------------ #
    # Persistent idempotency + cross-process single-writer discipline
    # ------------------------------------------------------------------ #
    def _initialize_idempotency_index(self) -> None:
        self._conn.executescript(
            f"""
            CREATE TABLE IF NOT EXISTS audit_idempotency (
                event_id TEXT PRIMARY KEY,
                audit_seq INTEGER NOT NULL UNIQUE,
                FOREIGN KEY(audit_seq) REFERENCES audit_log(seq)
            );
            CREATE TRIGGER IF NOT EXISTS audit_idempotency_no_update
            BEFORE UPDATE ON audit_idempotency BEGIN
                SELECT RAISE(
                    ABORT,
                    'audit_idempotency is append-only (WORM): UPDATE blocked'
                );
            END;
            CREATE TRIGGER IF NOT EXISTS audit_idempotency_no_delete
            BEFORE DELETE ON audit_idempotency BEGIN
                SELECT RAISE(
                    ABORT,
                    'audit_idempotency is append-only (WORM): DELETE blocked'
                );
            END;
            CREATE TRIGGER IF NOT EXISTS audit_log_index_idempotency
            AFTER INSERT ON audit_log
            WHEN json_type(
                NEW.event_json,
                '$.metadata.{_IDEMPOTENCY_METADATA_KEY}'
            ) = 'text'
            BEGIN
                INSERT INTO audit_idempotency(event_id, audit_seq)
                VALUES (
                    json_extract(
                        NEW.event_json,
                        '$.metadata.{_IDEMPOTENCY_METADATA_KEY}'
                    ),
                    NEW.seq
                );
            END;
            """
        )
        self._backfill_idempotency_index()

    def _backfill_idempotency_index(self) -> None:
        self._conn.execute(
            f"""
            INSERT OR IGNORE INTO audit_idempotency(event_id, audit_seq)
            SELECT
                json_extract(event_json, '$.metadata.{_IDEMPOTENCY_METADATA_KEY}'),
                seq
            FROM audit_log
            WHERE json_type(
                event_json,
                '$.metadata.{_IDEMPOTENCY_METADATA_KEY}'
            ) = 'text'
            """
        )
        self._conn.commit()

    def _has_idempotency_marker(self, event_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM audit_idempotency WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        return row is not None

    def _advance_external_anchor(self, event_id: str) -> None:
        """Acknowledge only the exact one-row commit whose anchor write was interrupted."""
        anchor_value = getattr(self._log, "_anchor_path", "")
        if not anchor_value:
            return
        anchor_path = Path(anchor_value)
        marker = self._conn.execute(
            "SELECT audit_seq FROM audit_idempotency WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        head = self._conn.execute(
            """
            SELECT seq, prev_hash, entry_hash
            FROM audit_log
            WHERE entry_hash IS NOT NULL
            ORDER BY seq DESC
            LIMIT 1
            """
        ).fetchone()
        if marker is None or head is None:
            raise AuditChainError("idempotent audit marker or chain head is missing")
        if not anchor_path.exists():
            if marker["audit_seq"] != 1 or head["seq"] != 1:
                raise AuditChainError(
                    "external audit anchor is missing for a multi-row chain; "
                    "refusing to recreate it"
                )
            report = self._log.verify_chain()
            if not report.ok:
                raise AuditChainError("audit chain is invalid; refusing to create its anchor")
            self._log._write_anchor()
            return
        try:
            anchor = json.loads(anchor_path.read_text(encoding="utf-8"))
            anchor_seq = anchor["seq"]
            anchor_hash = anchor["entry_hash"]
            if (
                not isinstance(anchor_seq, int)
                or isinstance(anchor_seq, bool)
                or anchor_seq < 1
                or not isinstance(anchor_hash, str)
                or not anchor_hash
            ):
                raise ValueError("invalid anchor fields")
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AuditChainError(
                "external audit anchor is invalid; refusing to replace it"
            ) from exc

        anchored_row = self._conn.execute(
            "SELECT entry_hash FROM audit_log WHERE seq = ? AND entry_hash IS NOT NULL",
            (anchor_seq,),
        ).fetchone()
        if anchored_row is None or anchored_row["entry_hash"] != anchor_hash:
            raise AuditChainError(
                "external audit anchor is not a prefix of the current chain; "
                "refusing to move it backward"
            )
        report = self._log.verify_chain()
        if report.first_bad_seq is not None:
            raise AuditChainError(
                f"audit chain is invalid at seq {report.first_bad_seq}; refusing to advance anchor"
            )
        if head["seq"] == anchor_seq:
            return
        if not (
            head["seq"] == anchor_seq + 1
            and marker["audit_seq"] == head["seq"]
            and head["prev_hash"] == anchor_hash
        ):
            raise AuditChainError(
                "external audit anchor can advance only for the exact next idempotent commit"
            )
        self._log._write_anchor()

    def _require_current_external_anchor(self) -> None:
        """Refuse an append when external evidence and the SQLite head diverge."""
        anchor_value = getattr(self._log, "_anchor_path", "")
        if not anchor_value:
            return
        anchor_path = Path(anchor_value)
        head = self._conn.execute(
            """
            SELECT seq, entry_hash
            FROM audit_log
            WHERE entry_hash IS NOT NULL
            ORDER BY seq DESC
            LIMIT 1
            """
        ).fetchone()
        if not anchor_path.exists():
            if head is None:
                return
            raise AuditChainError(
                "external audit anchor is missing for a non-empty chain; refusing to append"
            )
        try:
            anchor = json.loads(anchor_path.read_text(encoding="utf-8"))
            anchor_seq = anchor["seq"]
            anchor_hash = anchor["entry_hash"]
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AuditChainError("external audit anchor is invalid; refusing to append") from exc
        if (
            not isinstance(anchor_seq, int)
            or isinstance(anchor_seq, bool)
            or not isinstance(anchor_hash, str)
            or head is None
            or head["seq"] != anchor_seq
            or head["entry_hash"] != anchor_hash
        ):
            raise AuditChainError(
                "external audit anchor does not match the current chain head; refusing to append"
            )
        report = self._log.verify_chain()
        if not report.ok:
            raise AuditChainError(f"audit chain is invalid; refusing to append: {report.detail}")

    @contextmanager
    def _exclusive_writer(self) -> Iterator[None]:
        """Serialize chain-head reads and appends across adapter processes."""
        with self._writer_thread_lock:
            if self._writer_lock_path is None:
                yield
                return
            self._writer_lock_path.parent.mkdir(parents=True, exist_ok=True)
            with self._writer_lock_path.open("a+b") as lock_file:
                _lock_file(lock_file)
                try:
                    yield
                finally:
                    _unlock_file(lock_file)


def _lock_file(lock_file: Any) -> None:
    if os.name == "nt":
        msvcrt = import_module("msvcrt")

        lock_file.seek(0)
        if lock_file.read(1) == b"":
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
        return
    import fcntl

    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)


def _unlock_file(lock_file: Any) -> None:
    if os.name == "nt":
        msvcrt = import_module("msvcrt")

        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
