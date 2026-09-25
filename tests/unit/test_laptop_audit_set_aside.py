"""A laptop reset is never refused by the audit ledger: an untrusted store is set aside.

Owner rule, 2026-09-23: the laptop run never refuses to start because audit-integrity machinery
objects to a reset. Before this file a laptop whose store had been damaged, had lost its anchor,
or had been restored from an older copy (rolled back behind its anchor) refused every append,
so the flagship could not record a single dossier until somebody deleted files by hand.

Each case below proves three things: the reset now starts, its first append lands and the fresh
store verifies (over MCP's ``audit_verify`` too); nothing was deleted, because the old store
and its anchor sit beside it under a ``.set-aside-<timestamp>`` name; and the set-aside trail
verifies EXACTLY as it did before the reset, so the evidence of what went wrong travels with
it. A run nobody chose a profile for keeps refusing, which is the posture every non-laptop
store keeps. Opening and verifying never set anything aside: an operator inspecting a laptop
store sees what is wrong.
"""

from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from hex_service_kit.audit import ChainReport, HashChainedAuditLog
from hex_service_kit.mcpserve import AUDIT_VERIFY_TOOL, audit_tools

from cdd_sow_research.adapters.local.audit import AuditChainError, LocalAppendOnlyAuditAdapter
from cdd_sow_research.config import LocalSettings, Settings
from cdd_sow_research.domain.models import AuditEvent, Decision

ANCHOR_ENV = "CDD_LOCAL_AUDIT_ANCHOR"


def _adapter(path: Path, *, laptop: bool = True) -> LocalAppendOnlyAuditAdapter:
    return LocalAppendOnlyAuditAdapter(
        Settings(
            profile="local",
            profile_explicit=laptop,
            local=LocalSettings(db_path=":memory:", audit_path=str(path)),
        )
    )


def _event(i: int) -> AuditEvent:
    return AuditEvent(
        action="assess_cdd",
        actor="analyst@bank.test",
        decision=Decision.ESCALATED,
        redacted_prompt=f"[PERSON_NAME] asked for dossier {i}",
        redacted_response=f"cited dossier summary {i}",
        trace_id=f"trace-{i}",
    )


def _set_aside(path: Path) -> list[Path]:
    return sorted(path.parent.glob(f"{path.name}.set-aside-*"))


def _verify_as_found(store: Path, anchor: Path | None) -> ChainReport:
    """Verify a trail with the shared engine alone, as anyone holding the files would."""
    log = HashChainedAuditLog(str(store), anchor_path=str(anchor) if anchor else "")
    try:
        return log.verify_chain()
    finally:
        log._conn.close()


def _without_detail_paths(report: ChainReport) -> tuple[bool, int, int, int | None]:
    return (report.ok, report.entries, report.chained, report.first_bad_seq)


def _assert_fresh_and_verifiable(audit: LocalAppendOnlyAuditAdapter) -> None:
    """The append that used to be refused lands, on a fresh chain that verifies."""
    audit.record(_event(100))
    after = audit.verify_chain()
    assert after.ok and after.entries == 1, after
    # And the read-only evidence tool an MCP client calls agrees.
    _, handlers = audit_tools(audit)
    over_mcp = handlers[AUDIT_VERIFY_TOOL]()
    assert over_mcp["ok"] is True and over_mcp["entries"] == 1, over_mcp


@pytest.fixture
def anchored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    store, anchor = tmp_path / "audit.db", tmp_path / "audit-head.anchor"
    monkeypatch.setenv(ANCHOR_ENV, str(anchor))
    audit = _adapter(store)
    for i in range(3):
        audit.record(_event(i))
    assert audit.verify_chain().ok and anchor.exists()
    audit._conn.close()
    return store, anchor


def _damage(store: Path) -> None:
    import sqlite3

    conn = sqlite3.connect(store)
    conn.execute("DROP TRIGGER audit_log_no_update")
    conn.execute("UPDATE audit_log SET event_json = '{\"forged\": true}' WHERE seq = 2")
    conn.commit()
    conn.close()


def _roll_back(store: Path) -> None:
    """Restore an older copy of the store, the anchor having moved on since."""
    older = store.with_name("older.db")
    shutil.copy(store, older)
    audit = _adapter(store)
    audit.record(_event(3))
    audit._conn.close()
    shutil.copy(older, store)
    older.unlink()


# --------------------------------------------------------------------------------------- #
# The three resets
# --------------------------------------------------------------------------------------- #
def test_a_damaged_store_is_set_aside_and_a_fresh_chain_starts(
    anchored: tuple[Path, Path],
) -> None:
    store, anchor = anchored
    _damage(store)
    before = _verify_as_found(store, anchor)
    assert not before.ok and before.first_bad_seq == 2

    audit = _adapter(store)
    # Inspection first: opening and verifying report the damage and move nothing.
    assert _without_detail_paths(audit.verify_chain()) == _without_detail_paths(before)
    assert _set_aside(store) == []

    _assert_fresh_and_verifiable(audit)
    [old_store] = _set_aside(store)
    [old_anchor] = _set_aside(anchor)
    after = _verify_as_found(old_store, old_anchor)
    assert _without_detail_paths(after) == _without_detail_paths(before)
    assert after.detail == before.detail


def test_an_unreadable_store_is_set_aside_and_kept_byte_for_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = tmp_path / "audit.db"
    monkeypatch.delenv(ANCHOR_ENV, raising=False)
    store.write_bytes(b"not a database at all, but somebody's evidence all the same")

    audit = _adapter(store)

    _assert_fresh_and_verifiable(audit)
    [old_store] = _set_aside(store)
    assert old_store.read_bytes() == b"not a database at all, but somebody's evidence all the same"


def test_a_store_whose_anchor_is_missing_is_set_aside(
    anchored: tuple[Path, Path],
) -> None:
    store, anchor = anchored
    anchor.unlink()
    before = _verify_as_found(store, anchor)
    assert not before.ok and "anchor" in before.detail

    audit = _adapter(store)

    _assert_fresh_and_verifiable(audit)
    [old_store] = _set_aside(store)
    # Nothing witnessed it, but the chain itself is whole: the set-aside trail verifies.
    assert _verify_as_found(old_store, None).ok
    # And checked against the anchor that is still missing, it reports what it did before.
    assert _without_detail_paths(_verify_as_found(old_store, anchor)) == _without_detail_paths(
        before
    )


def test_a_store_rolled_back_behind_its_anchor_is_set_aside(
    anchored: tuple[Path, Path],
) -> None:
    store, anchor = anchored
    _roll_back(store)
    before = _verify_as_found(store, anchor)
    assert not before.ok and "anchor" in before.detail

    audit = _adapter(store)

    _assert_fresh_and_verifiable(audit)
    [old_store] = _set_aside(store)
    [old_anchor] = _set_aside(anchor)
    # The older copy's chain is intact on its own, and its moved anchor still exposes the
    # rollback exactly as it did in place.
    assert _verify_as_found(old_store, None).ok
    after = _verify_as_found(old_store, old_anchor)
    assert _without_detail_paths(after) == _without_detail_paths(before)
    assert after.detail == before.detail


# --------------------------------------------------------------------------------------- #
# What is NOT set aside
# --------------------------------------------------------------------------------------- #
def test_an_intact_store_is_reopened_not_set_aside(anchored: tuple[Path, Path]) -> None:
    store, anchor = anchored

    audit = _adapter(store)

    assert audit.verify_chain().ok and audit.verify_chain().entries == 3
    assert _set_aside(store) == [] and _set_aside(anchor) == []


def test_a_run_nobody_chose_a_profile_for_still_refuses(
    anchored: tuple[Path, Path],
) -> None:
    """The leniency is a laptop relaxation; outside it the rolled-back store is refused."""
    store, anchor = anchored
    _roll_back(store)

    audit = _adapter(store, laptop=False)

    with pytest.raises(AuditChainError):
        audit.record(_event(99))
    assert _set_aside(store) == [] and _set_aside(anchor) == []


def test_every_laptop_profile_gets_the_leniency_and_no_other() -> None:
    base = Settings(profile_explicit=True)
    assert {
        p
        for p in ("local", "live", "gcp", "platform", "onprem")
        if replace(base, profile=p).laptop_run
    } == {"local", "live"}
