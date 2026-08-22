"""Transactional SQLite implementation of ``BrowserFlowStorePort``."""

from __future__ import annotations

import hmac
import json
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
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

_FLOW_TABLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS browser_flows (
    record_id TEXT PRIMARY KEY,
    flow_kind TEXT NOT NULL CHECK (
        flow_kind IN ('citation_continuation', 'embedded_grant')
    ),
    ticket_hash TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK (
        state IN ('REGISTERED', 'AUTH_PENDING', 'CODE_ISSUED', 'CONSUMED', 'EXPIRED')
    ),
    installation_id TEXT NOT NULL,
    tenant TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    state_changed_at TEXT NOT NULL,
    source_actor TEXT,
    expected_actor TEXT,
    case_id TEXT,
    evidence_id TEXT,
    citation_id TEXT,
    auth_transaction_id TEXT,
    protocol_version TEXT,
    pkce_challenge TEXT,
    pkce_method TEXT,
    client_id TEXT,
    source_issuer TEXT,
    source_subject TEXT,
    scopes_json TEXT,
    subject_expires_at TEXT,
    code_hash TEXT UNIQUE,
    code_issued_at TEXT,
    code_expires_at TEXT
);
"""

_OUTBOX_TABLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS browser_flow_outbox (
    event_id TEXT PRIMARY KEY,
    record_id TEXT NOT NULL,
    flow_kind TEXT NOT NULL,
    state TEXT NOT NULL,
    installation_id TEXT NOT NULL,
    tenant TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    delivered_at TEXT,
    FOREIGN KEY(record_id) REFERENCES browser_flows(record_id)
);
"""

_CITATION_LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS cdd_citation_ledger (
    citation_id TEXT NOT NULL,
    tenant TEXT NOT NULL,
    source_actor TEXT NOT NULL,
    case_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    page INTEGER,
    PRIMARY KEY (citation_id, tenant, source_actor)
);
"""

_INDEX_SCHEMA = """
CREATE INDEX IF NOT EXISTS browser_flows_expiry
    ON browser_flows(state, expires_at);
CREATE INDEX IF NOT EXISTS browser_flow_outbox_pending
    ON browser_flow_outbox(delivered_at, occurred_at, event_id);
"""


class LocalSQLiteBrowserFlowStore:
    """Single-replica, restart-safe browser-flow store for local proof.

    Each operation gets its own SQLite connection and acquires ``BEGIN IMMEDIATE`` before
    reading state.  This gives compare-and-transition semantics across threads and across
    the isolated and standalone local processes.  Production and multi-replica use fail
    at construction because this adapter is not the shared production implementation.
    """

    def __init__(
        self,
        database_path: Settings | str | Path,
        *,
        production: bool | None = None,
        replica_count: int | None = None,
    ) -> None:
        from ...config import Settings

        if isinstance(database_path, Settings):
            settings = database_path
            database_path = settings.local.browser_flow_path
            production = settings.deployment.production if production is None else production
            replica_count = (
                settings.deployment.replica_count if replica_count is None else replica_count
            )
        production = bool(production)
        replica_count = 1 if replica_count is None else replica_count
        if production:
            raise ValueError("local SQLite browser-flow store is disabled in production")
        if replica_count != 1:
            raise ValueError("local SQLite browser-flow store requires exactly one replica")
        if not str(database_path).strip():
            raise ValueError(
                "local.browser_flow_path must explicitly name the shared browser-flow database"
            )
        path = Path(database_path).expanduser()
        if str(path) in {"", ":memory:"}:
            raise ValueError("browser-flow store requires a durable filesystem path")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._database_path = str(path)
        self._initialize()

    def record_citations(self, entries: tuple[CitationLedgerEntry, ...]) -> None:
        """Persist only citations that were actually emitted to the verified actor."""
        with self._transaction() as connection:
            for entry in entries:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO cdd_citation_ledger (
                        citation_id, tenant, source_actor, case_id,
                        evidence_id, source_id, page
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        entry.citation_id,
                        entry.tenant,
                        entry.source_actor,
                        entry.case_id,
                        entry.evidence_id,
                        entry.source_id,
                        entry.page,
                    ),
                )
                stored = connection.execute(
                    """
                    SELECT * FROM cdd_citation_ledger
                    WHERE citation_id = ? AND tenant = ? AND source_actor = ?
                    """,
                    (entry.citation_id, entry.tenant, entry.source_actor),
                ).fetchone()
                if stored is None or self._citation_from_row(stored) != entry:
                    raise BrowserFlowBindingError(
                        "citation identifier is already bound to different evidence"
                    )

    def get_citation(
        self,
        citation_id: str,
        *,
        tenant: str,
        source_actor: str,
    ) -> CitationLedgerEntry:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM cdd_citation_ledger
                WHERE citation_id = ? AND tenant = ? AND source_actor = ?
                """,
                (citation_id, tenant, source_actor),
            ).fetchone()
        if row is None:
            raise BrowserFlowNotFoundError("citation was not emitted to this actor")
        return self._citation_from_row(row)

    def register_citation(
        self,
        registration: CitationFlowRegistration,
        *,
        now: datetime,
        expires_at: datetime,
    ) -> RegisteredBrowserFlow:
        """Create a high-entropy ticket while persisting only its SHA-256 hash."""

        for _attempt in range(5):
            opaque_token = secrets.token_urlsafe(32)
            record_id = secrets.token_urlsafe(24)
            record = new_citation_flow(
                record_id=record_id,
                ticket_hash=hash_opaque_token(opaque_token),
                registration=registration,
                created_at=now,
                expires_at=expires_at,
            )
            try:
                with self._transaction() as connection:
                    self._insert_record(connection, record)
                    self._insert_outbox(connection, browser_flow_event(record))
            except sqlite3.IntegrityError:
                continue
            return RegisteredBrowserFlow(record=record, opaque_token=opaque_token)
        raise BrowserFlowOutboxError("could not allocate a unique browser-flow reference")

    def begin_citation(
        self,
        opaque_ticket: str,
        *,
        auth_transaction_id: str,
        as_of: datetime,
    ) -> CitationContinuationRecord:
        ticket_hash = hash_opaque_token(opaque_ticket)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM browser_flows WHERE ticket_hash = ?",
                (ticket_hash,),
            ).fetchone()
            if row is None:
                raise BrowserFlowNotFoundError("citation ticket is unknown")
            current = self._record_from_row(row)
            if not isinstance(current, CitationContinuationRecord):
                raise BrowserFlowNotFoundError("citation ticket is unknown")
            if _utc(as_of) >= current.expires_at:
                updated = transition_citation_flow(current, BrowserFlowState.EXPIRED, as_of=as_of)
            else:
                updated = transition_citation_flow(
                    current,
                    BrowserFlowState.AUTH_PENDING,
                    as_of=as_of,
                    auth_transaction_id=auth_transaction_id,
                )
            self._persist_transition(connection, current, updated)
            return updated

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
        with self._transaction() as connection:
            current = self._load_record(connection, record_id)
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
            if _utc(as_of) >= current.expires_at:
                updated = transition_citation_flow(current, BrowserFlowState.EXPIRED, as_of=as_of)
            else:
                updated = transition_citation_flow(
                    current,
                    BrowserFlowState.CONSUMED,
                    as_of=as_of,
                    auth_transaction_id=auth_transaction_id,
                )
            self._persist_transition(connection, current, updated)
            return updated

    def register_grant(
        self,
        registration: GrantFlowRegistration,
        *,
        now: datetime,
        expires_at: datetime,
    ) -> RegisteredGrantInstance:
        """Create an opaque instance while persisting only its digest."""

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
                with self._transaction() as connection:
                    self._insert_record(connection, record)
                    self._insert_outbox(connection, browser_flow_event(record))
            except sqlite3.IntegrityError:
                continue
            return RegisteredGrantInstance(record=record, opaque_instance_id=opaque_instance_id)
        raise BrowserFlowOutboxError("could not allocate a unique grant instance")

    def authorize_grant(
        self,
        opaque_instance_id: str,
        authorization: GrantAuthorization,
        *,
        as_of: datetime,
    ) -> IssuedGrantCode:
        """Atomically authorize once and return one unrecoverable launch code."""

        instance_hash = hash_opaque_token(opaque_instance_id)
        for _attempt in range(5):
            launch_code = secrets.token_urlsafe(32)
            expired = False
            issued: EmbeddedGrantRecord | None = None
            try:
                with self._transaction() as connection:
                    current = self._load_grant_by_instance_hash(connection, instance_hash)
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
                        updated = transition_grant_flow(
                            current, BrowserFlowState.EXPIRED, as_of=as_of
                        )
                    else:
                        updated = authorize_grant_flow(
                            current,
                            authorization,
                            code_hash=hash_opaque_token(launch_code),
                            as_of=as_of,
                        )
                        issued = updated
                    self._persist_transition(connection, current, updated)
            except sqlite3.IntegrityError:
                continue
            if expired:
                raise BrowserFlowExpiredError("grant registration or subject has expired")
            assert issued is not None
            return IssuedGrantCode(record=issued, launch_code=launch_code)
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
        """Atomically consume one instance/code/verifier combination."""

        instance_hash = hash_opaque_token(opaque_instance_id)
        presented_code_hash = hash_opaque_token(launch_code)
        expired = False
        consumed: EmbeddedGrantRecord | None = None
        with self._transaction() as connection:
            current = self._load_grant_by_instance_hash(connection, instance_hash)
            if current.state is not BrowserFlowState.CODE_ISSUED:
                raise BrowserFlowStateError("grant consume requires CODE_ISSUED state")
            assert current.code_hash is not None
            assert current.code_expires_at is not None
            if _utc(as_of) >= min(current.expires_at, current.code_expires_at):
                expired = True
                updated = transition_grant_flow(current, BrowserFlowState.EXPIRED, as_of=as_of)
            else:
                if not (
                    _same(installation_id, current.registration.installation_id)
                    and _same(presented_code_hash, current.code_hash)
                    and pkce_verifier_matches(current.registration.pkce_challenge, pkce_verifier)
                ):
                    raise BrowserFlowBindingError(
                        "grant instance, code, verifier, or installation does not match"
                    )
                updated = transition_grant_flow(current, BrowserFlowState.CONSUMED, as_of=as_of)
                consumed = updated
            self._persist_transition(connection, current, updated)
        if expired:
            raise BrowserFlowExpiredError("grant code has expired")
        assert consumed is not None
        return consumed

    def expire(self, record_id: str, *, as_of: datetime) -> BrowserFlowRecord:
        with self._transaction() as connection:
            current = self._load_record(connection, record_id)
            if isinstance(current, CitationContinuationRecord):
                updated: BrowserFlowRecord = transition_citation_flow(
                    current, BrowserFlowState.EXPIRED, as_of=as_of
                )
            else:
                updated = transition_grant_flow(current, BrowserFlowState.EXPIRED, as_of=as_of)
            self._persist_transition(connection, current, updated)
            return updated

    def get(self, record_id: str) -> BrowserFlowRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM browser_flows WHERE record_id = ?",
                (record_id,),
            ).fetchone()
        if row is None:
            raise BrowserFlowNotFoundError("browser-flow record is unknown")
        return self._record_from_row(row)

    def pending_outbox(self, *, limit: int = 100) -> tuple[BrowserFlowOutboxEvent, ...]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
            raise ValueError("outbox limit must be an integer from 1 to 1000")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM browser_flow_outbox
                WHERE delivered_at IS NULL
                ORDER BY occurred_at, event_id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return tuple(self._event_from_row(row) for row in rows)

    def mark_outbox_delivered(
        self, event_id: str, *, delivered_at: datetime
    ) -> BrowserFlowOutboxEvent:
        delivered_at = _utc(delivered_at)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM browser_flow_outbox WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if row is None:
                raise BrowserFlowOutboxError("outbox event is unknown")
            current = self._event_from_row(row)
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
            connection.execute(
                """
                UPDATE browser_flow_outbox
                SET delivered_at = ?
                WHERE event_id = ? AND delivered_at IS NULL
                """,
                (_timestamp(delivered_at), event_id),
            )
            return acknowledged

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            existing = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'browser_flows'"
            ).fetchone()
            if existing is not None and "embedded_grant" not in existing["sql"]:
                self._migrate_citation_schema(connection)
            connection.executescript(
                _FLOW_TABLE_SCHEMA + _OUTBOX_TABLE_SCHEMA + _CITATION_LEDGER_SCHEMA + _INDEX_SCHEMA
            )

    @staticmethod
    def _migrate_citation_schema(connection: sqlite3.Connection) -> None:
        """Upgrade the unreleased P2 citation schema without losing pending flows."""

        connection.execute("PRAGMA foreign_keys = OFF")
        connection.executescript(
            """
            BEGIN IMMEDIATE;
            ALTER TABLE browser_flow_outbox RENAME TO browser_flow_outbox_v1;
            ALTER TABLE browser_flows RENAME TO browser_flows_v1;
            """
            + _FLOW_TABLE_SCHEMA
            + _OUTBOX_TABLE_SCHEMA
            + """
            INSERT INTO browser_flows (
                record_id, flow_kind, ticket_hash, state, installation_id, tenant,
                source_actor, expected_actor, case_id, evidence_id, citation_id,
                correlation_id, created_at, expires_at, state_changed_at,
                auth_transaction_id
            )
            SELECT
                record_id, flow_kind, ticket_hash, state, installation_id, tenant,
                source_actor, expected_actor, case_id, evidence_id, citation_id,
                correlation_id, created_at, expires_at, state_changed_at,
                auth_transaction_id
            FROM browser_flows_v1;
            INSERT INTO browser_flow_outbox
            SELECT * FROM browser_flow_outbox_v1;
            DROP TABLE browser_flow_outbox_v1;
            DROP TABLE browser_flows_v1;
            """
            + _INDEX_SCHEMA
            + "COMMIT;"
        )
        connection.execute("PRAGMA foreign_keys = ON")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._database_path,
            isolation_level=None,
            timeout=30,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @staticmethod
    def _insert_record(connection: sqlite3.Connection, record: BrowserFlowRecord) -> None:
        if isinstance(record, CitationContinuationRecord):
            citation_registration = record.registration
            connection.execute(
                """
                INSERT INTO browser_flows (
                    record_id, flow_kind, ticket_hash, state, installation_id, tenant,
                    source_actor, expected_actor, case_id, evidence_id, citation_id,
                    correlation_id, created_at, expires_at, state_changed_at,
                    auth_transaction_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.record_id,
                    record.kind.value,
                    record.ticket_hash,
                    record.state.value,
                    citation_registration.installation_id,
                    citation_registration.tenant,
                    citation_registration.source_actor,
                    citation_registration.expected_actor,
                    citation_registration.case_id,
                    citation_registration.evidence_id,
                    citation_registration.citation_id,
                    citation_registration.correlation_id,
                    _timestamp(record.created_at),
                    _timestamp(record.expires_at),
                    _timestamp(record.state_changed_at),
                    record.auth_transaction_id,
                ),
            )
            return
        grant_registration = record.registration
        connection.execute(
            """
            INSERT INTO browser_flows (
                record_id, flow_kind, ticket_hash, state, installation_id, tenant,
                correlation_id, created_at, expires_at, state_changed_at,
                protocol_version, pkce_challenge, pkce_method
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.record_id,
                record.kind.value,
                record.instance_hash,
                record.state.value,
                grant_registration.installation_id,
                grant_registration.tenant,
                grant_registration.correlation_id,
                _timestamp(record.created_at),
                _timestamp(record.expires_at),
                _timestamp(record.state_changed_at),
                grant_registration.protocol_version,
                grant_registration.pkce_challenge,
                grant_registration.pkce_method,
            ),
        )

    @staticmethod
    def _insert_outbox(connection: sqlite3.Connection, event: BrowserFlowOutboxEvent) -> None:
        connection.execute(
            """
            INSERT INTO browser_flow_outbox (
                event_id, record_id, flow_kind, state, installation_id, tenant,
                correlation_id, occurred_at, delivered_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.record_id,
                event.flow_kind.value,
                event.state.value,
                event.installation_id,
                event.tenant,
                event.correlation_id,
                _timestamp(event.occurred_at),
                None,
            ),
        )

    def _persist_transition(
        self,
        connection: sqlite3.Connection,
        current: BrowserFlowRecord,
        updated: BrowserFlowRecord,
    ) -> None:
        if isinstance(updated, CitationContinuationRecord):
            cursor = connection.execute(
                """
                UPDATE browser_flows
                SET state = ?, state_changed_at = ?, auth_transaction_id = ?
                WHERE record_id = ? AND state = ? AND flow_kind = ?
                """,
                (
                    updated.state.value,
                    _timestamp(updated.state_changed_at),
                    updated.auth_transaction_id,
                    updated.record_id,
                    current.state.value,
                    BrowserFlowKind.CITATION_CONTINUATION.value,
                ),
            )
        else:
            authorization = updated.authorization
            cursor = connection.execute(
                """
                UPDATE browser_flows
                SET state = ?, state_changed_at = ?, client_id = ?,
                    source_issuer = ?, source_subject = ?, scopes_json = ?,
                    subject_expires_at = ?, code_hash = ?, code_issued_at = ?,
                    code_expires_at = ?
                WHERE record_id = ? AND state = ? AND flow_kind = ?
                """,
                (
                    updated.state.value,
                    _timestamp(updated.state_changed_at),
                    authorization.client_id if authorization else None,
                    authorization.source_issuer if authorization else None,
                    authorization.source_subject if authorization else None,
                    (
                        json.dumps(authorization.scopes, separators=(",", ":"))
                        if authorization
                        else None
                    ),
                    (_timestamp(authorization.subject_expires_at) if authorization else None),
                    updated.code_hash,
                    (_timestamp(updated.code_issued_at) if updated.code_issued_at else None),
                    (_timestamp(updated.code_expires_at) if updated.code_expires_at else None),
                    updated.record_id,
                    current.state.value,
                    BrowserFlowKind.EMBEDDED_GRANT.value,
                ),
            )
        if cursor.rowcount != 1:
            raise BrowserFlowStateError("browser-flow state changed concurrently")
        self._insert_outbox(connection, browser_flow_event(updated))

    def _load_record(self, connection: sqlite3.Connection, record_id: str) -> BrowserFlowRecord:
        row = connection.execute(
            "SELECT * FROM browser_flows WHERE record_id = ?",
            (record_id,),
        ).fetchone()
        if row is None:
            raise BrowserFlowNotFoundError("browser-flow record is unknown")
        return self._record_from_row(row)

    def _load_grant_by_instance_hash(
        self, connection: sqlite3.Connection, instance_hash: str
    ) -> EmbeddedGrantRecord:
        row = connection.execute(
            """
            SELECT * FROM browser_flows
            WHERE ticket_hash = ? AND flow_kind = ?
            """,
            (instance_hash, BrowserFlowKind.EMBEDDED_GRANT.value),
        ).fetchone()
        if row is None:
            raise BrowserFlowNotFoundError("grant instance is unknown")
        record = self._record_from_row(row)
        assert isinstance(record, EmbeddedGrantRecord)
        return record

    @staticmethod
    def _record_from_row(row: sqlite3.Row) -> BrowserFlowRecord:
        kind = BrowserFlowKind(row["flow_kind"])
        if kind is BrowserFlowKind.EMBEDDED_GRANT:
            grant_registration = GrantFlowRegistration(
                installation_id=row["installation_id"],
                tenant=row["tenant"],
                protocol_version=row["protocol_version"],
                pkce_challenge=row["pkce_challenge"],
                pkce_method=row["pkce_method"],
                correlation_id=row["correlation_id"],
            )
            authorization = None
            if row["client_id"] is not None:
                raw_scopes = json.loads(row["scopes_json"])
                if not isinstance(raw_scopes, list) or not all(
                    isinstance(scope, str) for scope in raw_scopes
                ):
                    raise BrowserFlowStateError("stored grant scopes are invalid")
                authorization = GrantAuthorization(
                    installation_id=row["installation_id"],
                    client_id=row["client_id"],
                    source_issuer=row["source_issuer"],
                    source_subject=row["source_subject"],
                    tenant=row["tenant"],
                    scopes=tuple(raw_scopes),
                    subject_expires_at=_parse_timestamp(row["subject_expires_at"]),
                )
            return EmbeddedGrantRecord(
                record_id=row["record_id"],
                instance_hash=row["ticket_hash"],
                state=BrowserFlowState(row["state"]),
                registration=grant_registration,
                created_at=_parse_timestamp(row["created_at"]),
                expires_at=_parse_timestamp(row["expires_at"]),
                state_changed_at=_parse_timestamp(row["state_changed_at"]),
                authorization=authorization,
                code_hash=row["code_hash"],
                code_issued_at=(
                    _parse_timestamp(row["code_issued_at"])
                    if row["code_issued_at"] is not None
                    else None
                ),
                code_expires_at=(
                    _parse_timestamp(row["code_expires_at"])
                    if row["code_expires_at"] is not None
                    else None
                ),
                kind=kind,
            )
        citation_registration = CitationFlowRegistration(
            installation_id=row["installation_id"],
            tenant=row["tenant"],
            source_actor=row["source_actor"],
            expected_actor=row["expected_actor"],
            case_id=row["case_id"],
            evidence_id=row["evidence_id"],
            citation_id=row["citation_id"],
            correlation_id=row["correlation_id"],
        )
        return CitationContinuationRecord(
            record_id=row["record_id"],
            ticket_hash=row["ticket_hash"],
            state=BrowserFlowState(row["state"]),
            registration=citation_registration,
            created_at=_parse_timestamp(row["created_at"]),
            expires_at=_parse_timestamp(row["expires_at"]),
            state_changed_at=_parse_timestamp(row["state_changed_at"]),
            auth_transaction_id=row["auth_transaction_id"],
            kind=kind,
        )

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> BrowserFlowOutboxEvent:
        delivered = row["delivered_at"]
        return BrowserFlowOutboxEvent(
            event_id=row["event_id"],
            record_id=row["record_id"],
            flow_kind=BrowserFlowKind(row["flow_kind"]),
            state=BrowserFlowState(row["state"]),
            installation_id=row["installation_id"],
            tenant=row["tenant"],
            correlation_id=row["correlation_id"],
            occurred_at=_parse_timestamp(row["occurred_at"]),
            delivered_at=_parse_timestamp(delivered) if delivered is not None else None,
        )

    @staticmethod
    def _citation_from_row(row: sqlite3.Row) -> CitationLedgerEntry:
        return CitationLedgerEntry(
            citation_id=row["citation_id"],
            tenant=row["tenant"],
            source_actor=row["source_actor"],
            case_id=row["case_id"],
            evidence_id=row["evidence_id"],
            source_id=row["source_id"],
            page=row["page"],
        )


def _same(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("time must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds")


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)
