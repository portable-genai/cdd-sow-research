"""Tamper-evident audit chain + open-format round-trip (P-08 / P-12).

The blog-level portability claim this suite makes concrete: the audit trail is an
append-only log in which every entry is cryptographically chained to its predecessor,
stored/exported in a plain documented format (JSON Lines), and a full export reloads on
a fresh store with every field and the chain intact.
"""

from __future__ import annotations

import json
import multiprocessing
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from cdd_sow_research.adapters.local.audit import (
    AuditChainError,
    LocalAppendOnlyAuditAdapter,
)
from cdd_sow_research.config import LocalSettings, Settings
from cdd_sow_research.domain.models import AuditEvent, Citation, Decision, SourceType
from cdd_sow_research.domain.serialization import audit_event_from_jsonable, to_jsonable


def _adapter(path: str = ":memory:") -> LocalAppendOnlyAuditAdapter:
    return LocalAppendOnlyAuditAdapter(
        Settings(local=LocalSettings(db_path=":memory:", audit_path=path))
    )


def _event(i: int) -> AuditEvent:
    return AuditEvent(
        action="assess_cdd",
        actor="analyst@bank.test",
        decision=Decision.ESCALATED,
        redacted_prompt=f"[PERSON_NAME] asked for dossier {i}",
        redacted_response=f"cited dossier summary {i}",
        citations=(
            Citation(
                source_id=f"doc-{i}",
                source_type=SourceType.DOCUMENT,
                title="Registry extract (FICTIONAL)",
                page=3,
            ),
        ),
        trace_id=f"trace-{i}",
        metadata={"case": f"case-{i}"},
    )


def _record_once_process(
    audit_path: str,
    event_id: str,
    serial: int,
    ready: Any,
    start: Any,
    crash_after_commit: bool = False,
) -> None:
    audit = _adapter(audit_path)
    ready.put(event_id)
    if not start.wait(timeout=10):
        raise RuntimeError("multiprocess audit test did not start")
    audit.record_once(event_id, replace(_event(serial), trace_id=event_id))
    if crash_after_commit:
        os._exit(23)


def _record_once_process_crashing_before_anchor(
    audit_path: str,
    anchor_path: str,
    event_id: str,
    ready: Any,
    start: Any,
) -> None:
    os.environ["CDD_LOCAL_AUDIT_ANCHOR"] = anchor_path
    audit = _adapter(audit_path)

    def crash_before_anchor_write() -> None:
        os._exit(23)

    audit._log._write_anchor = crash_before_anchor_write
    ready.put(event_id)
    if not start.wait(timeout=10):
        raise RuntimeError("multiprocess audit test did not start")
    audit.record_once(event_id, replace(_event(6), trace_id=event_id))


def _run_record_processes(
    audit_path: Path,
    identities: list[tuple[str, int]],
) -> list[multiprocessing.Process]:
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    start = context.Event()
    processes = [
        context.Process(
            target=_record_once_process,
            args=(str(audit_path), event_id, serial, ready, start),
        )
        for event_id, serial in identities
    ]
    for process in processes:
        process.start()
    for _process in processes:
        ready.get(timeout=10)
    start.set()
    for process in processes:
        process.join(timeout=15)
        assert not process.is_alive()
        assert process.exitcode == 0
    return processes


def test_chain_intact_after_records():
    audit = _adapter()
    for i in range(5):
        audit.record(_event(i))
    report = audit.verify_chain()
    assert report.ok, report.detail
    assert report.entries == 5
    assert report.chained == 5
    assert report.legacy == 0


def test_in_place_tamper_is_detected():
    audit = _adapter()
    for i in range(3):
        audit.record(_event(i))
    # Simulate an attacker editing a stored record in place (bypassing the adapter).
    audit._conn.execute("DROP TRIGGER audit_log_no_update")
    audit._conn.execute(
        "UPDATE audit_log SET event_json = replace(event_json, 'dossier summary 1', 'DOCTORED') "
        "WHERE seq = 2"
    )
    audit._conn.commit()
    report = audit.verify_chain()
    assert not report.ok
    assert report.first_bad_seq == 2
    assert "altered" in report.detail


def test_deleting_a_record_breaks_the_chain():
    audit = _adapter()
    for i in range(3):
        audit.record(_event(i))
    audit._conn.execute("DROP TRIGGER audit_log_no_delete")
    audit._conn.execute("DELETE FROM audit_log WHERE seq = 2")
    audit._conn.commit()
    report = audit.verify_chain()
    assert not report.ok
    assert report.first_bad_seq == 3


def test_export_reload_round_trip(tmp_path: Path):
    source = _adapter(str(tmp_path / "audit-a.db"))
    events = [_event(i) for i in range(4)]
    for event in events:
        source.record(event)

    export_path = tmp_path / "audit-export.jsonl"
    assert source.export_jsonl(export_path) == 4

    target = _adapter(str(tmp_path / "audit-b.db"))
    assert target.import_jsonl(export_path) == 4

    report = target.verify_chain()
    assert report.ok and report.chained == 4

    # Every field survives the round-trip, including rehydration to the domain type.
    assert target.read_all() == source.read_all()
    reloaded = [audit_event_from_jsonable(payload) for payload in target.read_all()]
    assert reloaded == events


def test_restore_refuses_non_empty_store(tmp_path: Path):
    source = _adapter(str(tmp_path / "audit-a.db"))
    source.record(_event(0))
    export_path = tmp_path / "export.jsonl"
    source.export_jsonl(export_path)

    non_empty = _adapter(str(tmp_path / "audit-b.db"))
    non_empty.record(_event(99))
    with pytest.raises(AuditChainError, match="non-empty"):
        non_empty.import_jsonl(export_path)


def test_restore_detects_transit_tamper(tmp_path: Path):
    source = _adapter(str(tmp_path / "audit-a.db"))
    for i in range(2):
        source.record(_event(i))
    export_path = tmp_path / "export.jsonl"
    source.export_jsonl(export_path)

    doctored = export_path.read_text().replace("cited dossier summary 1", "DOCTORED")
    export_path.write_text(doctored)

    target = _adapter(str(tmp_path / "audit-b.db"))
    with pytest.raises(AuditChainError, match="altered in transit"):
        target.import_jsonl(export_path)


def test_legacy_pre_chain_rows_are_counted_and_the_trail_is_not_reported_intact(tmp_path: Path):
    """A store created before chaining existed upgrades in place, but is NOT blessed.

    An unhashed row is indistinguishable from one a direct INSERT fabricated, so it is
    unverifiable and the report says so: ``ok`` is False with the count in ``legacy``. This
    assertion is the inverse of the one this suite once carried, which reported such a trail
    intact as long as every HASHED row linked up: an attacker who could INSERT could therefore
    add rows that verification waved through.
    """
    import sqlite3

    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    # A store created before chaining existed: the shared engine's base schema (event_json only),
    # with no hash columns. The adapter adds them on open; the old row stays unverifiable (legacy).
    conn.execute(
        "CREATE TABLE audit_log (seq INTEGER PRIMARY KEY AUTOINCREMENT, event_json TEXT NOT NULL)"
    )
    conn.execute("INSERT INTO audit_log (event_json) VALUES ('{}')")
    conn.commit()
    conn.close()

    audit = _adapter(str(db))
    audit.record(_event(1))
    report = audit.verify_chain()
    assert not report.ok
    assert report.legacy == 1
    assert report.chained == 1
    assert "no chain hashes" in report.detail
    # It stays caught: a later legitimate append does not buy back "chain intact".
    audit.record(_event(2))
    assert not audit.verify_chain().ok


def test_audit_event_jsonable_round_trip():
    event = _event(7)
    assert audit_event_from_jsonable(to_jsonable(event)) == event


def test_worm_triggers_block_update_and_delete():
    import sqlite3 as _sqlite3

    import pytest as _pytest

    audit = _adapter()
    audit.record(_event(1))
    with _pytest.raises(_sqlite3.IntegrityError, match="append-only"):
        audit._conn.execute("UPDATE audit_log SET event_json = '{}' WHERE seq = 1")
    with _pytest.raises(_sqlite3.IntegrityError, match="append-only"):
        audit._conn.execute("DELETE FROM audit_log WHERE seq = 1")


def test_external_anchor_detects_tail_truncation(tmp_path: Path, monkeypatch):
    anchor = tmp_path / "audit-head.anchor"
    monkeypatch.setenv("CDD_LOCAL_AUDIT_ANCHOR", str(anchor))
    audit = _adapter(str(tmp_path / "audit.db"))
    for i in range(3):
        audit.record(_event(i))
    assert audit.verify_chain().ok

    # Truncate the tail: drop the newest row. The remaining chain is still internally
    # valid, so only the anchored head exposes the truncation.
    audit._conn.execute("DROP TRIGGER audit_log_no_delete")
    audit._conn.execute("DELETE FROM audit_log WHERE seq = (SELECT MAX(seq) FROM audit_log)")
    audit._conn.commit()
    report = audit.verify_chain()
    assert not report.ok
    assert "anchor" in report.detail


def test_record_once_is_durable_across_restart_and_duplicate_delivery(tmp_path: Path):
    path = tmp_path / "audit.db"
    event_id = "browser-flow-event-" + ("a" * 32)
    first = _adapter(str(path))
    first.record_once(event_id, replace(_event(1), trace_id=event_id))

    restarted = _adapter(str(path))
    restarted.record_once(event_id, replace(_event(99), trace_id=event_id))

    records = restarted.read_all()
    assert len(records) == 1
    assert records[0]["trace_id"] == event_id
    assert records[0]["metadata"]["_audit_event_id"] == event_id
    assert restarted.verify_chain().ok


def test_record_once_serializes_two_process_chain_appends(tmp_path: Path):
    path = tmp_path / "audit.db"
    first_id = "browser-flow-event-" + ("a" * 32)
    second_id = "browser-flow-event-" + ("b" * 32)

    _run_record_processes(path, [(first_id, 1), (second_id, 2)])

    restarted = _adapter(str(path))
    records = restarted.read_all()
    assert {record["trace_id"] for record in records} == {first_id, second_id}
    report = restarted.verify_chain()
    assert report.ok
    assert report.entries == 2


def test_record_once_two_process_duplicate_creates_one_record(tmp_path: Path):
    path = tmp_path / "audit.db"
    event_id = "browser-flow-event-" + ("c" * 32)

    _run_record_processes(path, [(event_id, 1), (event_id, 2)])

    restarted = _adapter(str(path))
    assert [record["trace_id"] for record in restarted.read_all()] == [event_id]
    assert restarted.verify_chain().ok


def test_record_once_crash_after_commit_is_retry_safe(tmp_path: Path):
    path = tmp_path / "audit.db"
    event_id = "browser-flow-event-" + ("d" * 32)
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    start = context.Event()
    process = context.Process(
        target=_record_once_process,
        args=(str(path), event_id, 3, ready, start, True),
    )
    process.start()
    ready.get(timeout=10)
    start.set()
    process.join(timeout=15)
    assert not process.is_alive()
    assert process.exitcode == 23

    restarted = _adapter(str(path))
    restarted.record_once(event_id, replace(_event(4), trace_id=event_id))

    records = restarted.read_all()
    assert len(records) == 1
    assert records[0]["trace_id"] == event_id
    assert "opaque-browser-ticket" not in json.dumps(records)
    assert restarted.verify_chain().ok


def test_record_once_redelivery_repairs_anchor_after_commit_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    path = tmp_path / "audit.db"
    anchor = tmp_path / "audit-head.anchor"
    monkeypatch.setenv("CDD_LOCAL_AUDIT_ANCHOR", str(anchor))
    first_id = "browser-flow-event-" + ("e" * 32)
    interrupted_id = "browser-flow-event-" + ("f" * 32)

    initial = _adapter(str(path))
    initial.record_once(first_id, replace(_event(5), trace_id=first_id))

    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    start = context.Event()
    process = context.Process(
        target=_record_once_process_crashing_before_anchor,
        args=(str(path), str(anchor), interrupted_id, ready, start),
    )
    process.start()
    ready.get(timeout=10)
    start.set()
    process.join(timeout=15)
    assert not process.is_alive()
    assert process.exitcode == 23

    restarted = _adapter(str(path))
    assert not restarted.verify_chain().ok

    restarted.record_once(interrupted_id, replace(_event(7), trace_id=interrupted_id))

    assert [record["trace_id"] for record in restarted.read_all()] == [
        first_id,
        interrupted_id,
    ]
    assert restarted.verify_chain().ok
    assert json.loads(anchor.read_text(encoding="utf-8"))["seq"] == 2


def test_record_once_duplicate_never_moves_anchor_backward_after_tail_truncation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    path = tmp_path / "audit.db"
    anchor = tmp_path / "audit-head.anchor"
    monkeypatch.setenv("CDD_LOCAL_AUDIT_ANCHOR", str(anchor))
    audit = _adapter(str(path))
    event_ids = [f"browser-flow-event-{suffix * 32}" for suffix in ("g", "h", "i")]
    for serial, event_id in enumerate(event_ids):
        audit.record_once(event_id, replace(_event(serial), trace_id=event_id))
    anchored_head = anchor.read_text(encoding="utf-8")

    audit._conn.execute("DROP TRIGGER audit_log_no_delete")
    audit._conn.execute("DELETE FROM audit_log WHERE seq = (SELECT MAX(seq) FROM audit_log)")
    audit._conn.commit()

    with pytest.raises(AuditChainError, match="refusing to move it backward"):
        audit.record_once(event_ids[0], replace(_event(99), trace_id=event_ids[0]))

    assert anchor.read_text(encoding="utf-8") == anchored_head
    report = audit.verify_chain()
    assert not report.ok
    assert "anchor" in report.detail


def test_record_once_old_duplicate_cannot_bless_a_consistent_unanchored_suffix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    path = tmp_path / "audit.db"
    anchor = tmp_path / "audit-head.anchor"
    monkeypatch.setenv("CDD_LOCAL_AUDIT_ANCHOR", str(anchor))
    audit = _adapter(str(path))
    anchored_id = "browser-flow-event-" + ("j" * 32)
    unanchored_id = "browser-flow-event-" + ("k" * 32)
    audit.record_once(anchored_id, replace(_event(1), trace_id=anchored_id))
    anchored_head = anchor.read_text(encoding="utf-8")

    # Force a MULTI-row suffix the witness never saw. Suppressing only the anchor write is
    # not enough: hex-service-kit refuses the second such append itself, since
    # the store head has already diverged from the anchor. Suppressing the continuity check
    # too models the threat the anchor exists for, an actor with write access to the store
    # file, and keeps this test about the redelivery path rather than the append path.
    original_write_anchor = audit._log._write_anchor
    original_assert_continuity = audit._log._assert_anchor_continuity
    monkeypatch.setattr(audit._log, "_write_anchor", lambda: None)
    monkeypatch.setattr(audit._log, "_assert_anchor_continuity", lambda: None)
    audit.record_once(unanchored_id, replace(_event(2), trace_id=unanchored_id))
    audit._log.record(_event(3))
    monkeypatch.setattr(audit._log, "_write_anchor", original_write_anchor)
    monkeypatch.setattr(audit._log, "_assert_anchor_continuity", original_assert_continuity)

    with pytest.raises(AuditChainError, match="exact next idempotent commit"):
        audit.record_once(anchored_id, replace(_event(4), trace_id=anchored_id))

    assert anchor.read_text(encoding="utf-8") == anchored_head
    assert not audit.verify_chain().ok


def test_ordinary_append_cannot_erase_external_tail_truncation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    path = tmp_path / "audit.db"
    anchor = tmp_path / "audit-head.anchor"
    monkeypatch.setenv("CDD_LOCAL_AUDIT_ANCHOR", str(anchor))
    audit = _adapter(str(path))
    for serial in range(3):
        audit.record(_event(serial))
    anchored_head = anchor.read_text(encoding="utf-8")

    audit._conn.execute("DROP TRIGGER audit_log_no_delete")
    audit._conn.execute("DELETE FROM audit_log WHERE seq = (SELECT MAX(seq) FROM audit_log)")
    audit._conn.commit()

    with pytest.raises(AuditChainError, match="does not match the current chain head"):
        audit.record(_event(99))

    assert anchor.read_text(encoding="utf-8") == anchored_head
    assert not audit.verify_chain().ok
