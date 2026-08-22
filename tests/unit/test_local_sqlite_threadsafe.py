"""Concurrency regression tests for the local SQLite-backed adapters.

The ``local serve`` API serves **sync** ``def`` endpoints from Starlette's anyio worker
threadpool, while ``api.deps.get_container`` is ``@lru_cache``d, so a single, process-wide
adapter instance (one ``sqlite3`` connection) is shared across worker threads. Without
``check_same_thread=False`` plus a lock serialising every connection access, a request
handled on a thread other than the one that opened the connection raises ``ProgrammingError:
SQLite objects created in a thread can only be used in that same thread`` (dropping the WORM
audit event / returning 500 under concurrent load).

These tests construct each adapter ONCE and drive interleaved writes and reads from many
threads via :class:`~concurrent.futures.ThreadPoolExecutor`, asserting no exception escapes
and the final row count is exactly right. They fail before the ``check_same_thread=False`` +
lock fix and pass after it.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from cdd_sow_research.adapters.local.audit import LocalAppendOnlyAuditAdapter
from cdd_sow_research.adapters.local.knowledge_base import LocalKnowledgeBaseAdapter
from cdd_sow_research.config import LocalSettings, Settings
from cdd_sow_research.domain.models import (
    AuditEvent,
    Citation,
    Decision,
    RetrievalQuery,
    RetrievedPassage,
    SourceType,
)

_N_THREADS = 8
_N_OPS = 25  # writes per thread


def _settings(db_path: str, audit_path: str) -> Settings:
    return Settings(
        profile="local",
        local=LocalSettings(db_path=db_path, audit_path=audit_path),
    )


def _audit_event(i: int) -> AuditEvent:
    return AuditEvent(
        action="assess_cdd",
        actor=f"analyst-{i}",
        decision=Decision.ALLOWED,
        redacted_prompt=f"prompt {i}",
        redacted_response=f"response {i}",
    )


def _passage(i: int) -> RetrievedPassage:
    return RetrievedPassage(
        text=f"source of wealth passage number {i} salary dividends",
        citation=Citation(
            source_id=f"doc-{i}",
            source_type=SourceType.DOCUMENT,
            title=f"KYC document {i}",
            page=1,
            score=0.5,
        ),
        score=0.5,
    )


def test_local_audit_threadsafe(tmp_path: Path) -> None:
    """record() + read_all() from many threads on one shared connection must not raise."""
    db = str(tmp_path / "audit.db")
    adapter = LocalAppendOnlyAuditAdapter(_settings(":memory:", db))

    errors: list[BaseException] = []

    def writer(start: int) -> None:
        try:
            for i in range(start, start + _N_OPS):
                adapter.record(_audit_event(i))
                adapter.read_all()  # interleave a cross-thread read
        except BaseException as exc:  # noqa: BLE001 - surface any thread error to the test
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=_N_THREADS) as pool:
        futures = [pool.submit(writer, t * _N_OPS) for t in range(_N_THREADS)]
        for f in as_completed(futures):
            f.result()

    assert not errors, f"cross-thread audit access raised: {errors[0]!r}"
    assert len(adapter.read_all()) == _N_THREADS * _N_OPS


def test_local_knowledge_base_threadsafe(tmp_path: Path) -> None:
    """ingest() (write) + search() (read) from many threads on one shared connection."""
    db = str(tmp_path / "kb.db")
    adapter = LocalKnowledgeBaseAdapter(_settings(db, ":memory:"))
    # Start from a known empty index so the final count is deterministic.
    adapter.seed([])

    errors: list[BaseException] = []
    query = RetrievalQuery(text="source of wealth salary", top_k=5)

    def worker(start: int) -> None:
        try:
            for i in range(start, start + _N_OPS):
                adapter.add([_passage(i)])
                adapter.search(query)  # interleave a cross-thread BM25 read
        except BaseException as exc:  # noqa: BLE001 - surface any thread error to the test
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=_N_THREADS) as pool:
        futures = [pool.submit(worker, t * _N_OPS) for t in range(_N_THREADS)]
        for f in as_completed(futures):
            f.result()

    assert not errors, f"cross-thread knowledge-base access raised: {errors[0]!r}"
    # Every passage carries a unique source_id, so all inserts must be present.
    big = RetrievalQuery(text="source of wealth passage salary dividends", top_k=10_000)
    assert len(adapter.search(big)) == _N_THREADS * _N_OPS
