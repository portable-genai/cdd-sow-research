"""Lifecycle dispatcher from browser-flow outbox transitions to immutable audit."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime

from ..domain.browser_flow import BrowserFlowOutboxEvent
from ..domain.models import AuditEvent, Decision
from ..ports.browser_flow_store import BrowserFlowStorePort
from ..ports.observability import AuditSinkPort

_LOG = logging.getLogger(__name__)


def audit_event(event: BrowserFlowOutboxEvent) -> AuditEvent:
    """Project only sanitized transition metadata, with the stable event ID."""
    return AuditEvent(
        action="browser_flow_transition",
        actor="system:browser-flow-outbox",
        decision=Decision.ALLOWED,
        redacted_prompt="",
        redacted_response="",
        resource=f"browser-flow:{event.flow_kind.value}",
        trace_id=event.event_id,
        timestamp=event.occurred_at,
        metadata={
            "event_id": event.event_id,
            "record_id": event.record_id,
            "flow_kind": event.flow_kind.value,
            "state": event.state.value,
            "installation_id": event.installation_id,
            "tenant": event.tenant,
            "correlation_id": event.correlation_id,
        },
    )


class BrowserFlowOutboxDispatcher:
    """Ordered, retrying, idempotent delivery with acknowledgement after success."""

    def __init__(
        self,
        store: BrowserFlowStorePort,
        audit: AuditSinkPort,
        *,
        poll_seconds: float = 0.5,
        max_backoff_seconds: float = 30.0,
        batch_size: int = 100,
    ) -> None:
        self._store = store
        self._audit = audit
        self._poll_seconds = poll_seconds
        self._max_backoff_seconds = max_backoff_seconds
        self._batch_size = batch_size

    def dispatch_once(self) -> int:
        """Deliver one ordered batch; stop at the first unavailable audit write."""
        delivered = 0
        for pending in self._store.pending_outbox(limit=self._batch_size):
            self._audit.record_once(pending.event_id, audit_event(pending))
            self._store.mark_outbox_delivered(
                pending.event_id,
                delivered_at=datetime.now(UTC),
            )
            delivered += 1
        return delivered

    async def run(self, stop: asyncio.Event) -> None:
        """Poll until shutdown, with bounded exponential retry after outages."""
        backoff = self._poll_seconds
        while not stop.is_set():
            try:
                delivered = await asyncio.to_thread(self.dispatch_once)
                backoff = self._poll_seconds
                delay = 0.0 if delivered >= self._batch_size else self._poll_seconds
            except Exception:  # noqa: BLE001 - sink outage must retry, not kill lifespan
                # Never interpolate the sink exception: an upstream response can contain
                # untrusted or sensitive text. Operators need only the stable condition.
                _LOG.warning("browser-flow audit dispatch failed; retrying")
                delay = backoff
                backoff = min(max(backoff * 2, self._poll_seconds), self._max_backoff_seconds)
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)
