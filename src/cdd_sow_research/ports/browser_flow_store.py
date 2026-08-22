"""Atomic persistence boundary for short-lived browser flows."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from ..domain.browser_flow import (
    BrowserFlowOutboxEvent,
    BrowserFlowRecord,
    CitationContinuationRecord,
    CitationFlowRegistration,
    CitationLedgerEntry,
    EmbeddedGrantRecord,
    GrantAuthorization,
    GrantFlowRegistration,
    IssuedGrantCode,
    RegisteredBrowserFlow,
    RegisteredGrantInstance,
)


@runtime_checkable
class BrowserFlowStorePort(Protocol):
    """Store opaque flow hashes, exact state, and transition outbox atomically."""

    def record_citations(self, entries: tuple[CitationLedgerEntry, ...]) -> None:
        """Idempotently persist citations actually emitted to verified actors."""
        ...

    def get_citation(
        self,
        citation_id: str,
        *,
        tenant: str,
        source_actor: str,
    ) -> CitationLedgerEntry:
        """Resolve an emitted citation for the exact verified actor and tenant."""
        ...

    def register_citation(
        self,
        registration: CitationFlowRegistration,
        *,
        now: datetime,
        expires_at: datetime,
    ) -> RegisteredBrowserFlow:
        """Create a citation ticket and return its plaintext exactly once."""
        ...

    def begin_citation(
        self,
        opaque_ticket: str,
        *,
        auth_transaction_id: str,
        as_of: datetime,
    ) -> CitationContinuationRecord:
        """Move the exact ticket from REGISTERED to AUTH_PENDING."""
        ...

    def consume_citation(
        self,
        record_id: str,
        *,
        actor: str,
        tenant: str,
        installation_id: str,
        auth_transaction_id: str,
        as_of: datetime,
    ) -> CitationContinuationRecord:
        """Consume once after exact callback identity and transaction binding."""
        ...

    def expire(self, record_id: str, *, as_of: datetime) -> BrowserFlowRecord:
        """Move a due live flow to EXPIRED."""
        ...

    def register_grant(
        self,
        registration: GrantFlowRegistration,
        *,
        now: datetime,
        expires_at: datetime,
    ) -> RegisteredGrantInstance:
        """Create a PKCE-bound instance and return its opaque ID exactly once."""
        ...

    def authorize_grant(
        self,
        opaque_instance_id: str,
        authorization: GrantAuthorization,
        *,
        as_of: datetime,
    ) -> IssuedGrantCode:
        """Authorize a REGISTERED instance and issue one launch code."""
        ...

    def consume_grant(
        self,
        opaque_instance_id: str,
        launch_code: str,
        pkce_verifier: str,
        *,
        installation_id: str,
        as_of: datetime,
    ) -> EmbeddedGrantRecord:
        """Consume one exact code using the instance-bound PKCE verifier."""
        ...

    def get(self, record_id: str) -> BrowserFlowRecord:
        """Read a persisted record without exposing its plaintext ticket."""
        ...

    def pending_outbox(self, *, limit: int = 100) -> tuple[BrowserFlowOutboxEvent, ...]:
        """Read undelivered events by stable event ID for idempotent dispatch."""
        ...

    def mark_outbox_delivered(
        self, event_id: str, *, delivered_at: datetime
    ) -> BrowserFlowOutboxEvent:
        """Acknowledge delivery; retries return the original acknowledgement."""
        ...
