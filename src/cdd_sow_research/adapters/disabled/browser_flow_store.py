"""Explicitly disabled browser-flow store for runtimes without shared persistence."""

from __future__ import annotations

from datetime import datetime

from ...config import Settings
from ...domain.browser_flow import (
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

_MESSAGE = (
    "BrowserFlowStorePort is disabled for this runtime; configure a reviewed shared "
    "production adapter before enabling cross-deployment browser flows"
)


class DisabledBrowserFlowStoreAdapter:
    """Fail-fast binding that makes an unavailable capability explicit."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def record_citations(self, entries: tuple[CitationLedgerEntry, ...]) -> None:
        raise NotImplementedError(_MESSAGE)

    def get_citation(
        self,
        citation_id: str,
        *,
        tenant: str,
        source_actor: str,
    ) -> CitationLedgerEntry:
        raise NotImplementedError(_MESSAGE)

    def register_citation(
        self,
        registration: CitationFlowRegistration,
        *,
        now: datetime,
        expires_at: datetime,
    ) -> RegisteredBrowserFlow:
        raise NotImplementedError(_MESSAGE)

    def begin_citation(
        self,
        opaque_ticket: str,
        *,
        auth_transaction_id: str,
        as_of: datetime,
    ) -> CitationContinuationRecord:
        raise NotImplementedError(_MESSAGE)

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
        raise NotImplementedError(_MESSAGE)

    def expire(self, record_id: str, *, as_of: datetime) -> BrowserFlowRecord:
        raise NotImplementedError(_MESSAGE)

    def register_grant(
        self,
        registration: GrantFlowRegistration,
        *,
        now: datetime,
        expires_at: datetime,
    ) -> RegisteredGrantInstance:
        raise NotImplementedError(_MESSAGE)

    def authorize_grant(
        self,
        opaque_instance_id: str,
        authorization: GrantAuthorization,
        *,
        as_of: datetime,
    ) -> IssuedGrantCode:
        raise NotImplementedError(_MESSAGE)

    def consume_grant(
        self,
        opaque_instance_id: str,
        launch_code: str,
        pkce_verifier: str,
        *,
        installation_id: str,
        as_of: datetime,
    ) -> EmbeddedGrantRecord:
        raise NotImplementedError(_MESSAGE)

    def get(self, record_id: str) -> BrowserFlowRecord:
        raise NotImplementedError(_MESSAGE)

    def pending_outbox(self, *, limit: int = 100) -> tuple[BrowserFlowOutboxEvent, ...]:
        raise NotImplementedError(_MESSAGE)

    def mark_outbox_delivered(
        self, event_id: str, *, delivered_at: datetime
    ) -> BrowserFlowOutboxEvent:
        raise NotImplementedError(_MESSAGE)
