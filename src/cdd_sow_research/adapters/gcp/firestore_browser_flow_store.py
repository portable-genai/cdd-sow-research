"""Regional transactional Firestore persistence for browser flows and replay."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, TypeVar

from ...config import Settings
from ...domain.browser_flow import (
    BrowserFlowBindingError,
    BrowserFlowExpiredError,
    BrowserFlowKind,
    BrowserFlowNotFoundError,
    BrowserFlowOutboxError,
    BrowserFlowOutboxEvent,
    BrowserFlowRecord,
    BrowserFlowState,
    BrowserFlowStateError,
    CitationContinuationRecord,
    CitationFlowRegistration,
    CitationLedgerEntry,
    EmbeddedGrantRecord,
    GrantAuthorization,
    GrantFlowRegistration,
    IssuedGrantCode,
    RegisteredBrowserFlow,
    RegisteredGrantInstance,
    authorize_grant_flow,
    browser_flow_event,
    hash_opaque_token,
    new_citation_flow,
    new_grant_flow,
    pkce_verifier_matches,
    transition_citation_flow,
    transition_grant_flow,
)
from ...domain.identity import IdentityError

_T = TypeVar("_T")


class _FirestoreCollections:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: Any | None = None

    def _db(self) -> Any:
        if self._client is None:
            from google.cloud import firestore

            self._client = firestore.Client(
                project=self._settings.project_id,
                database=self._settings.browser_flow_store.database,
            )
        return self._client

    def _collection(self, configured_name: str) -> Any:
        return self._db().collection(configured_name)

    def _transaction(self, callback: Callable[[Any], _T]) -> _T:
        from google.cloud import firestore

        transaction = self._db().transaction()

        @firestore.transactional
        def _run(txn: Any) -> _T:
            return callback(txn)

        return _run(transaction)


class FirestoreBrowserFlowStoreAdapter(_FirestoreCollections):
    """Multi-replica store with atomic state transitions and sanitized outbox writes."""

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        configured = settings.browser_flow_store
        self._records = configured.records_collection
        self._aliases = configured.aliases_collection
        self._outbox = configured.outbox_collection
        self._citations = configured.citations_collection

    def record_citations(self, entries: tuple[CitationLedgerEntry, ...]) -> None:
        """Atomically bind the complete emitted citation set or bind none of it."""

        def _write(txn: Any) -> None:
            pending: dict[str, tuple[Any, dict[str, Any]]] = {}
            for entry in entries:
                key = _citation_key(entry)
                ref = self._collection(self._citations).document(key)
                snap = ref.get(transaction=txn)
                data = _citation_document(entry)
                if snap.exists:
                    if snap.to_dict() != data:
                        raise BrowserFlowBindingError(
                            "citation identifier is already bound to different evidence"
                        )
                else:
                    queued = pending.get(key)
                    if queued is not None and queued[1] != data:
                        raise BrowserFlowBindingError(
                            "citation identifier is already bound to different evidence"
                        )
                    pending[key] = (ref, data)
            for ref, data in pending.values():
                txn.create(ref, data)

        self._transaction(_write)

    def get_citation(
        self,
        citation_id: str,
        *,
        tenant: str,
        source_actor: str,
    ) -> CitationLedgerEntry:
        key = _digest_key((citation_id, tenant, source_actor))
        snap = self._collection(self._citations).document(key).get()
        if not snap.exists:
            raise BrowserFlowNotFoundError("citation was not emitted to this actor")
        return _citation_from_document(snap.to_dict())

    def register_citation(
        self,
        registration: CitationFlowRegistration,
        *,
        now: datetime,
        expires_at: datetime,
    ) -> RegisteredBrowserFlow:
        for _attempt in range(5):
            opaque_token = secrets.token_urlsafe(32)
            record = new_citation_flow(
                record_id=secrets.token_urlsafe(24),
                ticket_hash=hash_opaque_token(opaque_token),
                registration=registration,
                created_at=now,
                expires_at=expires_at,
            )
            try:
                self._create_record(record)
            except Exception as exc:  # noqa: BLE001
                if _is_collision(exc):
                    continue
                raise
            return RegisteredBrowserFlow(record=record, opaque_token=opaque_token)
        raise BrowserFlowOutboxError("could not allocate a unique browser-flow reference")

    def begin_citation(
        self,
        opaque_ticket: str,
        *,
        auth_transaction_id: str,
        as_of: datetime,
    ) -> CitationContinuationRecord:
        record = self._transition_by_alias(
            _alias_id(BrowserFlowKind.CITATION_CONTINUATION, hash_opaque_token(opaque_ticket)),
            lambda current: self._begin_citation(
                current,
                auth_transaction_id=auth_transaction_id,
                as_of=as_of,
            ),
        )
        if not isinstance(record, CitationContinuationRecord):
            raise BrowserFlowNotFoundError("citation ticket is unknown")
        return record

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
        def _update(current: BrowserFlowRecord) -> BrowserFlowRecord:
            if not isinstance(current, CitationContinuationRecord):
                raise BrowserFlowNotFoundError("citation flow is unknown")
            registration = current.registration
            if not (
                _same(actor, registration.expected_actor)
                and _same(tenant, registration.tenant)
                and _same(installation_id, registration.installation_id)
            ):
                raise BrowserFlowBindingError(
                    "callback actor, tenant, or installation does not match"
                )
            target = (
                BrowserFlowState.EXPIRED
                if _utc(as_of) >= current.expires_at
                else BrowserFlowState.CONSUMED
            )
            return transition_citation_flow(
                current,
                target,
                as_of=as_of,
                auth_transaction_id=(
                    auth_transaction_id if target is BrowserFlowState.CONSUMED else None
                ),
            )

        record = self._transition_record(record_id, _update)
        assert isinstance(record, CitationContinuationRecord)
        return record

    def expire(self, record_id: str, *, as_of: datetime) -> BrowserFlowRecord:
        def _expire(current: BrowserFlowRecord) -> BrowserFlowRecord:
            if isinstance(current, CitationContinuationRecord):
                return transition_citation_flow(current, BrowserFlowState.EXPIRED, as_of=as_of)
            return transition_grant_flow(current, BrowserFlowState.EXPIRED, as_of=as_of)

        return self._transition_record(record_id, _expire)

    def register_grant(
        self,
        registration: GrantFlowRegistration,
        *,
        now: datetime,
        expires_at: datetime,
    ) -> RegisteredGrantInstance:
        for _attempt in range(5):
            opaque_instance_id = secrets.token_urlsafe(32)
            record = new_grant_flow(
                record_id=secrets.token_urlsafe(24),
                instance_hash=hash_opaque_token(opaque_instance_id),
                registration=registration,
                created_at=now,
                expires_at=expires_at,
            )
            try:
                self._create_record(record)
            except Exception as exc:  # noqa: BLE001
                if _is_collision(exc):
                    continue
                raise
            return RegisteredGrantInstance(
                record=record,
                opaque_instance_id=opaque_instance_id,
            )
        raise BrowserFlowOutboxError("could not allocate a unique grant instance")

    def authorize_grant(
        self,
        opaque_instance_id: str,
        authorization: GrantAuthorization,
        *,
        as_of: datetime,
    ) -> IssuedGrantCode:
        alias = _alias_id(
            BrowserFlowKind.EMBEDDED_GRANT,
            hash_opaque_token(opaque_instance_id),
        )
        for _attempt in range(5):
            launch_code = secrets.token_urlsafe(32)
            launch_code_hash = hash_opaque_token(launch_code)
            expired = False

            def _authorize(
                current: BrowserFlowRecord,
                code_hash: str = launch_code_hash,
            ) -> BrowserFlowRecord:
                nonlocal expired
                if not isinstance(current, EmbeddedGrantRecord):
                    raise BrowserFlowNotFoundError("grant instance is unknown")
                if not (
                    _same(
                        authorization.installation_id,
                        current.registration.installation_id,
                    )
                    and _same(authorization.tenant, current.registration.tenant)
                ):
                    raise BrowserFlowBindingError("grant installation or tenant does not match")
                if (
                    _utc(as_of) >= current.expires_at
                    or _utc(as_of) >= authorization.subject_expires_at
                ):
                    expired = True
                    return transition_grant_flow(current, BrowserFlowState.EXPIRED, as_of=as_of)
                return authorize_grant_flow(
                    current,
                    authorization,
                    code_hash=code_hash,
                    as_of=as_of,
                )

            try:
                updated = self._transition_by_alias(
                    alias,
                    _authorize,
                    code_alias=launch_code_hash,
                )
            except Exception as exc:  # noqa: BLE001
                if _is_collision(exc):
                    continue
                raise
            if expired:
                raise BrowserFlowExpiredError("grant registration or subject has expired")
            assert isinstance(updated, EmbeddedGrantRecord)
            return IssuedGrantCode(record=updated, launch_code=launch_code)
        raise BrowserFlowOutboxError("could not allocate a unique launch code")

    def consume_grant(
        self,
        opaque_instance_id: str,
        launch_code: str,
        pkce_verifier: str,
        *,
        installation_id: str,
        as_of: datetime,
    ) -> EmbeddedGrantRecord:
        expired = False
        alias = _alias_id(
            BrowserFlowKind.EMBEDDED_GRANT,
            hash_opaque_token(opaque_instance_id),
        )

        def _consume(current: BrowserFlowRecord) -> BrowserFlowRecord:
            nonlocal expired
            if not isinstance(current, EmbeddedGrantRecord):
                raise BrowserFlowNotFoundError("grant instance is unknown")
            if current.state is not BrowserFlowState.CODE_ISSUED:
                raise BrowserFlowStateError("grant consume requires CODE_ISSUED state")
            assert current.code_hash is not None
            assert current.code_expires_at is not None
            if _utc(as_of) >= min(current.expires_at, current.code_expires_at):
                expired = True
                return transition_grant_flow(current, BrowserFlowState.EXPIRED, as_of=as_of)
            if not (
                _same(installation_id, current.registration.installation_id)
                and _same(hash_opaque_token(launch_code), current.code_hash)
                and pkce_verifier_matches(current.registration.pkce_challenge, pkce_verifier)
            ):
                raise BrowserFlowBindingError(
                    "grant instance, code, verifier, or installation does not match"
                )
            return transition_grant_flow(current, BrowserFlowState.CONSUMED, as_of=as_of)

        updated = self._transition_by_alias(alias, _consume)
        if expired:
            raise BrowserFlowExpiredError("grant code has expired")
        assert isinstance(updated, EmbeddedGrantRecord)
        return updated

    def get(self, record_id: str) -> BrowserFlowRecord:
        snap = self._collection(self._records).document(record_id).get()
        if not snap.exists:
            raise BrowserFlowNotFoundError("browser-flow record is unknown")
        return _record_from_document(snap.to_dict())

    def pending_outbox(self, *, limit: int = 100) -> tuple[BrowserFlowOutboxEvent, ...]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
            raise ValueError("outbox limit must be an integer from 1 to 1000")
        from google.cloud.firestore_v1 import FieldFilter

        query = (
            self._collection(self._outbox)
            .where(filter=FieldFilter("delivered_at", "==", None))
            .order_by("occurred_at")
            .order_by("event_id")
            .limit(limit)
        )
        return tuple(_event_from_document(snap.to_dict()) for snap in query.stream())

    def mark_outbox_delivered(
        self, event_id: str, *, delivered_at: datetime
    ) -> BrowserFlowOutboxEvent:
        delivered_at = _utc(delivered_at)
        ref = self._collection(self._outbox).document(event_id)

        def _ack(txn: Any) -> BrowserFlowOutboxEvent:
            snap = ref.get(transaction=txn)
            if not snap.exists:
                raise BrowserFlowOutboxError("outbox event is unknown")
            current = _event_from_document(snap.to_dict())
            if current.delivered_at is not None:
                return current
            acknowledged = BrowserFlowOutboxEvent(
                event_id=current.event_id,
                record_id=current.record_id,
                flow_kind=current.flow_kind,
                state=current.state,
                installation_id=current.installation_id,
                tenant=current.tenant,
                correlation_id=current.correlation_id,
                occurred_at=current.occurred_at,
                delivered_at=delivered_at,
            )
            txn.update(ref, {"delivered_at": delivered_at})
            return acknowledged

        return self._transaction(_ack)

    def _create_record(self, record: BrowserFlowRecord) -> None:
        record_ref = self._collection(self._records).document(record.record_id)
        alias_ref = self._collection(self._aliases).document(_record_alias(record))
        event = browser_flow_event(record)
        event_ref = self._collection(self._outbox).document(event.event_id)

        def _create(txn: Any) -> None:
            txn.create(record_ref, _record_document(record))
            txn.create(
                alias_ref,
                {
                    "record_id": record.record_id,
                    "flow_kind": record.kind.value,
                    "expires_at": record.expires_at,
                },
            )
            txn.create(event_ref, _event_document(event))

        self._transaction(_create)

    def _transition_by_alias(
        self,
        alias_id: str,
        update: Callable[[BrowserFlowRecord], BrowserFlowRecord],
        *,
        code_alias: str | None = None,
    ) -> BrowserFlowRecord:
        alias_ref = self._collection(self._aliases).document(alias_id)

        def _transition(txn: Any) -> BrowserFlowRecord:
            alias_snap = alias_ref.get(transaction=txn)
            if not alias_snap.exists:
                raise BrowserFlowNotFoundError("browser-flow reference is unknown")
            return self._transition_in_transaction(
                txn,
                str(alias_snap.to_dict()["record_id"]),
                update,
                code_alias=code_alias,
            )

        return self._transaction(_transition)

    def _transition_record(
        self,
        record_id: str,
        update: Callable[[BrowserFlowRecord], BrowserFlowRecord],
    ) -> BrowserFlowRecord:
        return self._transaction(
            lambda txn: self._transition_in_transaction(txn, record_id, update)
        )

    def _transition_in_transaction(
        self,
        txn: Any,
        record_id: str,
        update: Callable[[BrowserFlowRecord], BrowserFlowRecord],
        *,
        code_alias: str | None = None,
    ) -> BrowserFlowRecord:
        record_ref = self._collection(self._records).document(record_id)
        snap = record_ref.get(transaction=txn)
        if not snap.exists:
            raise BrowserFlowNotFoundError("browser-flow record is unknown")
        current = _record_from_document(snap.to_dict())
        updated = update(current)
        if current.state is updated.state:
            raise BrowserFlowStateError("browser-flow state did not change")
        event = browser_flow_event(updated)
        txn.update(record_ref, _record_document(updated))
        if code_alias:
            txn.create(
                self._collection(self._aliases).document(
                    _alias_id(BrowserFlowKind.EMBEDDED_GRANT, code_alias, prefix="code")
                ),
                {
                    "record_id": record_id,
                    "flow_kind": BrowserFlowKind.EMBEDDED_GRANT.value,
                    "expires_at": updated.expires_at,
                },
            )
        txn.create(
            self._collection(self._outbox).document(event.event_id),
            _event_document(event),
        )
        return updated

    @staticmethod
    def _begin_citation(
        current: BrowserFlowRecord,
        *,
        auth_transaction_id: str,
        as_of: datetime,
    ) -> BrowserFlowRecord:
        if not isinstance(current, CitationContinuationRecord):
            raise BrowserFlowNotFoundError("citation ticket is unknown")
        if _utc(as_of) >= current.expires_at:
            return transition_citation_flow(current, BrowserFlowState.EXPIRED, as_of=as_of)
        return transition_citation_flow(
            current,
            BrowserFlowState.AUTH_PENDING,
            as_of=as_of,
            auth_transaction_id=auth_transaction_id,
        )


class FirestoreClientAssertionReplayStore(_FirestoreCollections):
    """Atomic regional replay boundary for Mode 5 private_key_jwt assertions."""

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self._replay = settings.browser_flow_store.replay_collection

    def consume(
        self,
        *,
        jti: str,
        client_id: str,
        expires_at: datetime,
        as_of: datetime,
    ) -> str:
        as_of = _utc(as_of)
        expires_at = _utc(expires_at)
        digest = hashlib.sha256(jti.encode()).hexdigest()
        ref = self._collection(self._replay).document(digest)

        def _consume(txn: Any) -> None:
            snap = ref.get(transaction=txn)
            if snap.exists:
                stored_expiry = _utc(snap.to_dict()["expires_at"])
                if stored_expiry > as_of:
                    raise IdentityError("private_key_jwt assertion has already been used")
                txn.update(
                    ref,
                    {
                        "client_id": client_id,
                        "expires_at": expires_at,
                        "consumed_at": as_of,
                    },
                )
            else:
                txn.create(
                    ref,
                    {
                        "client_id": client_id,
                        "expires_at": expires_at,
                        "consumed_at": as_of,
                    },
                )

        self._transaction(_consume)
        return digest


def _record_document(record: BrowserFlowRecord) -> dict[str, Any]:
    if isinstance(record, CitationContinuationRecord):
        citation_registration = record.registration
        return {
            "record_id": record.record_id,
            "flow_kind": record.kind.value,
            "ticket_hash": record.ticket_hash,
            "state": record.state.value,
            "registration": {
                "installation_id": citation_registration.installation_id,
                "tenant": citation_registration.tenant,
                "source_actor": citation_registration.source_actor,
                "expected_actor": citation_registration.expected_actor,
                "case_id": citation_registration.case_id,
                "evidence_id": citation_registration.evidence_id,
                "citation_id": citation_registration.citation_id,
                "correlation_id": citation_registration.correlation_id,
            },
            "created_at": record.created_at,
            "expires_at": record.expires_at,
            "state_changed_at": record.state_changed_at,
            "auth_transaction_id": record.auth_transaction_id,
        }
    grant_registration = record.registration
    authorization = record.authorization
    return {
        "record_id": record.record_id,
        "flow_kind": record.kind.value,
        "ticket_hash": record.instance_hash,
        "state": record.state.value,
        "registration": {
            "installation_id": grant_registration.installation_id,
            "tenant": grant_registration.tenant,
            "protocol_version": grant_registration.protocol_version,
            "pkce_challenge": grant_registration.pkce_challenge,
            "pkce_method": grant_registration.pkce_method,
            "correlation_id": grant_registration.correlation_id,
        },
        "authorization": (
            {
                "installation_id": authorization.installation_id,
                "client_id": authorization.client_id,
                "source_issuer": authorization.source_issuer,
                "source_subject": authorization.source_subject,
                "tenant": authorization.tenant,
                "scopes": list(authorization.scopes),
                "subject_expires_at": authorization.subject_expires_at,
            }
            if authorization
            else None
        ),
        "created_at": record.created_at,
        "expires_at": record.expires_at,
        "state_changed_at": record.state_changed_at,
        "code_hash": record.code_hash,
        "code_issued_at": record.code_issued_at,
        "code_expires_at": record.code_expires_at,
    }


def _record_from_document(data: dict[str, Any]) -> BrowserFlowRecord:
    kind = BrowserFlowKind(data["flow_kind"])
    registration = data["registration"]
    if kind is BrowserFlowKind.CITATION_CONTINUATION:
        return CitationContinuationRecord(
            record_id=data["record_id"],
            ticket_hash=data["ticket_hash"],
            state=BrowserFlowState(data["state"]),
            registration=CitationFlowRegistration(**registration),
            created_at=_utc(data["created_at"]),
            expires_at=_utc(data["expires_at"]),
            state_changed_at=_utc(data["state_changed_at"]),
            auth_transaction_id=data.get("auth_transaction_id"),
            kind=kind,
        )
    authorization_data = data.get("authorization")
    authorization = (
        GrantAuthorization(
            **{
                **authorization_data,
                "scopes": tuple(authorization_data["scopes"]),
                "subject_expires_at": _utc(authorization_data["subject_expires_at"]),
            }
        )
        if authorization_data
        else None
    )
    return EmbeddedGrantRecord(
        record_id=data["record_id"],
        instance_hash=data["ticket_hash"],
        state=BrowserFlowState(data["state"]),
        registration=GrantFlowRegistration(**registration),
        created_at=_utc(data["created_at"]),
        expires_at=_utc(data["expires_at"]),
        state_changed_at=_utc(data["state_changed_at"]),
        authorization=authorization,
        code_hash=data.get("code_hash"),
        code_issued_at=(_utc(data["code_issued_at"]) if data.get("code_issued_at") else None),
        code_expires_at=(_utc(data["code_expires_at"]) if data.get("code_expires_at") else None),
        kind=kind,
    )


def _event_document(event: BrowserFlowOutboxEvent) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "record_id": event.record_id,
        "flow_kind": event.flow_kind.value,
        "state": event.state.value,
        "installation_id": event.installation_id,
        "tenant": event.tenant,
        "correlation_id": event.correlation_id,
        "occurred_at": event.occurred_at,
        "delivered_at": event.delivered_at,
    }


def _event_from_document(data: dict[str, Any]) -> BrowserFlowOutboxEvent:
    return BrowserFlowOutboxEvent(
        event_id=data["event_id"],
        record_id=data["record_id"],
        flow_kind=BrowserFlowKind(data["flow_kind"]),
        state=BrowserFlowState(data["state"]),
        installation_id=data["installation_id"],
        tenant=data["tenant"],
        correlation_id=data["correlation_id"],
        occurred_at=_utc(data["occurred_at"]),
        delivered_at=(_utc(data["delivered_at"]) if data.get("delivered_at") else None),
    )


def _citation_document(entry: CitationLedgerEntry) -> dict[str, Any]:
    return {
        "citation_id": entry.citation_id,
        "tenant": entry.tenant,
        "source_actor": entry.source_actor,
        "case_id": entry.case_id,
        "evidence_id": entry.evidence_id,
        "source_id": entry.source_id,
        "page": entry.page,
    }


def _citation_from_document(data: dict[str, Any]) -> CitationLedgerEntry:
    return CitationLedgerEntry(**data)


def _record_alias(record: BrowserFlowRecord) -> str:
    digest = (
        record.ticket_hash
        if isinstance(record, CitationContinuationRecord)
        else record.instance_hash
    )
    return _alias_id(record.kind, digest)


def _alias_id(
    kind: BrowserFlowKind,
    digest: str,
    *,
    prefix: str = "instance",
) -> str:
    return f"{prefix}:{kind.value}:{digest}"


def _citation_key(entry: CitationLedgerEntry) -> str:
    return _digest_key((entry.citation_id, entry.tenant, entry.source_actor))


def _digest_key(parts: tuple[str, ...]) -> str:
    return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()


def _same(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode(), right.encode())


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("time must be timezone-aware")
    return value.astimezone(UTC)


def _is_collision(exc: Exception) -> bool:
    from google.api_core.exceptions import AlreadyExists, Conflict

    return isinstance(exc, (AlreadyExists, Conflict))
