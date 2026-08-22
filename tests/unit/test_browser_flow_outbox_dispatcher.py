"""Regression coverage for durable, retry-safe browser-flow audit dispatch."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from cdd_sow_research.api.browser_flow_outbox import (
    BrowserFlowOutboxDispatcher,
    audit_event,
)
from cdd_sow_research.domain.browser_flow import (
    BrowserFlowKind,
    BrowserFlowOutboxEvent,
    BrowserFlowState,
)


def _event() -> BrowserFlowOutboxEvent:
    return BrowserFlowOutboxEvent(
        event_id="browser-event-1",
        record_id="opaque-record-1",
        flow_kind=BrowserFlowKind.EMBEDDED_GRANT,
        state=BrowserFlowState.CONSUMED,
        installation_id="inst-demo-bank",
        tenant="demo-bank",
        correlation_id="correlation-1",
        occurred_at=datetime(2026, 7, 27, tzinfo=UTC),
    )


class _Store:
    def __init__(self, event: BrowserFlowOutboxEvent, *, fail_ack_once: bool = False) -> None:
        self.event = event
        self.delivered = False
        self.fail_ack_once = fail_ack_once

    def pending_outbox(self, *, limit: int = 100):
        return () if self.delivered else (self.event,)

    def mark_outbox_delivered(self, event_id: str, *, delivered_at: datetime):
        assert event_id == self.event.event_id
        if self.fail_ack_once:
            self.fail_ack_once = False
            raise RuntimeError("simulated acknowledgement outage")
        self.delivered = True
        return self.event


class _Audit:
    def __init__(self, *, fail_once: bool = False) -> None:
        self.fail_once = fail_once
        self.calls = 0
        self.records = {}

    def record(self, event) -> None:
        raise AssertionError("dispatcher must use the idempotent audit operation")

    def record_once(self, event_id: str, event) -> None:
        self.calls += 1
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("simulated audit outage")
        self.records.setdefault(event_id, event)


def test_dispatch_retries_outage_restart_and_duplicate_without_secret_payload() -> None:
    event = _event()
    store = _Store(event)
    audit = _Audit(fail_once=True)
    dispatcher = BrowserFlowOutboxDispatcher(store, audit)

    with pytest.raises(RuntimeError, match="audit outage"):
        dispatcher.dispatch_once()
    assert not store.delivered
    assert dispatcher.dispatch_once() == 1
    assert store.delivered
    assert BrowserFlowOutboxDispatcher(store, audit).dispatch_once() == 0

    crash_window_store = _Store(event, fail_ack_once=True)
    duplicate_safe_audit = _Audit()
    restarted = BrowserFlowOutboxDispatcher(crash_window_store, duplicate_safe_audit)
    with pytest.raises(RuntimeError, match="acknowledgement outage"):
        restarted.dispatch_once()
    assert restarted.dispatch_once() == 1
    assert duplicate_safe_audit.calls == 2
    assert len(duplicate_safe_audit.records) == 1

    projected = audit_event(event)
    assert projected.trace_id == event.event_id
    assert projected.redacted_prompt == projected.redacted_response == ""
    serialized = repr(projected)
    for forbidden in ("opaque-token", "pkce", "source_subject", "client_assertion"):
        assert forbidden not in serialized


@pytest.mark.asyncio
async def test_dispatcher_shutdown_is_clean_and_bounded() -> None:
    store = _Store(_event())
    store.delivered = True
    dispatcher = BrowserFlowOutboxDispatcher(store, _Audit(), poll_seconds=0.001)
    stop = asyncio.Event()
    task = asyncio.create_task(dispatcher.run(stop))
    await asyncio.sleep(0.005)
    stop.set()
    await asyncio.wait_for(task, timeout=1)
