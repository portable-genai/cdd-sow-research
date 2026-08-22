from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cdd_sow_research.adapters.local.browser_flow_store import LocalSQLiteBrowserFlowStore
from cdd_sow_research.domain.browser_flow import (
    BrowserFlowBindingError,
    BrowserFlowExpiredError,
    BrowserFlowKind,
    BrowserFlowNotExpiredError,
    BrowserFlowNotFoundError,
    BrowserFlowOutboxError,
    BrowserFlowState,
    BrowserFlowStateError,
    CitationFlowRegistration,
    CitationLedgerEntry,
    EmbeddedGrantRecord,
    GrantAuthorization,
    GrantFlowRegistration,
    authorize_grant_flow,
    hash_opaque_token,
    new_citation_flow,
    new_grant_flow,
    pkce_s256,
    transition_citation_flow,
    transition_grant_flow,
)
from cdd_sow_research.ports.browser_flow_store import BrowserFlowStorePort

NOW = datetime(2026, 7, 27, 10, 0, tzinfo=UTC)
PKCE_VERIFIER = "A" * 43


def _registration() -> CitationFlowRegistration:
    return CitationFlowRegistration(
        installation_id="inst_demo_bank",
        tenant="demo-bank",
        source_actor="https://embed-idp.example|subject-123",
        expected_actor="https://workforce-idp.example|subject-789",
        case_id="case-fictional-123",
        evidence_id="evidence-fictional-456",
        citation_id="citation-fictional-789",
        correlation_id="corr-001",
    )


def _store(tmp_path: Path) -> LocalSQLiteBrowserFlowStore:
    return LocalSQLiteBrowserFlowStore(tmp_path / "browser-flows.sqlite3")


def _register(store: LocalSQLiteBrowserFlowStore, *, lifetime: timedelta = timedelta(seconds=60)):
    return store.register_citation(
        _registration(),
        now=NOW,
        expires_at=NOW + lifetime,
    )


def test_citation_ledger_is_exact_actor_bound_and_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    entry = CitationLedgerEntry(
        citation_id="c1.emitted-citation",
        tenant="demo-bank",
        source_actor="issub:embedded-actor",
        case_id="case-123",
        evidence_id="doc-123",
        source_id="doc-123",
        page=2,
    )

    store.record_citations((entry,))
    store.record_citations((entry,))

    assert (
        store.get_citation(
            entry.citation_id,
            tenant=entry.tenant,
            source_actor=entry.source_actor,
        )
        == entry
    )
    with pytest.raises(BrowserFlowNotFoundError):
        store.get_citation(
            entry.citation_id,
            tenant=entry.tenant,
            source_actor="issub:different-actor",
        )
    with pytest.raises(BrowserFlowBindingError):
        store.record_citations(
            (
                CitationLedgerEntry(
                    citation_id=entry.citation_id,
                    tenant=entry.tenant,
                    source_actor=entry.source_actor,
                    case_id=entry.case_id,
                    evidence_id="doc-other",
                    source_id="doc-other",
                    page=entry.page,
                ),
            )
        )


def test_pure_citation_state_machine_is_deterministic_and_exact() -> None:
    record = new_citation_flow(
        record_id="flow-001",
        ticket_hash=hash_opaque_token("opaque-ticket"),
        registration=_registration(),
        created_at=NOW,
        expires_at=NOW + timedelta(seconds=60),
    )

    first = transition_citation_flow(
        record,
        BrowserFlowState.AUTH_PENDING,
        as_of=NOW + timedelta(seconds=1),
        auth_transaction_id="oidc-transaction-1",
    )
    repeated = transition_citation_flow(
        record,
        BrowserFlowState.AUTH_PENDING,
        as_of=NOW + timedelta(seconds=1),
        auth_transaction_id="oidc-transaction-1",
    )
    consumed = transition_citation_flow(
        first,
        BrowserFlowState.CONSUMED,
        as_of=NOW + timedelta(seconds=2),
        auth_transaction_id="oidc-transaction-1",
    )

    assert first == repeated
    assert consumed.state is BrowserFlowState.CONSUMED
    with pytest.raises(BrowserFlowStateError):
        transition_citation_flow(
            record,
            BrowserFlowState.CONSUMED,
            as_of=NOW + timedelta(seconds=1),
            auth_transaction_id="oidc-transaction-1",
        )


def test_register_persists_hash_only_and_atomic_outbox(tmp_path: Path) -> None:
    store = _store(tmp_path)

    registered = _register(store)

    assert isinstance(store, BrowserFlowStorePort)
    assert registered.record.state is BrowserFlowState.REGISTERED
    assert registered.record.ticket_hash == hash_opaque_token(registered.opaque_token)
    assert registered.opaque_token not in repr(registered.record)
    assert registered.opaque_token not in repr(registered)
    with sqlite3.connect(tmp_path / "browser-flows.sqlite3") as connection:
        row = connection.execute(
            "SELECT ticket_hash FROM browser_flows WHERE record_id = ?",
            (registered.record.record_id,),
        ).fetchone()
    assert row == (registered.record.ticket_hash,)
    events = store.pending_outbox()
    assert len(events) == 1
    assert events[0].state is BrowserFlowState.REGISTERED
    assert not hasattr(events[0], "source_actor")
    assert not hasattr(events[0], "citation_id")


def test_begin_and_consume_bind_exact_identity_and_transaction(tmp_path: Path) -> None:
    store = _store(tmp_path)
    registered = _register(store)
    pending = store.begin_citation(
        registered.opaque_token,
        auth_transaction_id="oidc-transaction-1",
        as_of=NOW + timedelta(seconds=1),
    )

    with pytest.raises(BrowserFlowBindingError):
        store.consume_citation(
            pending.record_id,
            actor="https://workforce-idp.example|wrong-subject",
            tenant="demo-bank",
            installation_id="inst_demo_bank",
            auth_transaction_id="oidc-transaction-1",
            as_of=NOW + timedelta(seconds=2),
        )
    assert store.get(pending.record_id).state is BrowserFlowState.AUTH_PENDING
    assert len(store.pending_outbox()) == 2

    consumed = store.consume_citation(
        pending.record_id,
        actor=_registration().expected_actor,
        tenant="demo-bank",
        installation_id="inst_demo_bank",
        auth_transaction_id="oidc-transaction-1",
        as_of=NOW + timedelta(seconds=2),
    )

    assert consumed.state is BrowserFlowState.CONSUMED
    with pytest.raises(BrowserFlowStateError):
        store.consume_citation(
            pending.record_id,
            actor=_registration().expected_actor,
            tenant="demo-bank",
            installation_id="inst_demo_bank",
            auth_transaction_id="oidc-transaction-1",
            as_of=NOW + timedelta(seconds=3),
        )


def test_late_operation_expires_once_and_cannot_replay(tmp_path: Path) -> None:
    store = _store(tmp_path)
    registered = _register(store, lifetime=timedelta(seconds=10))

    expired = store.begin_citation(
        registered.opaque_token,
        auth_transaction_id="oidc-transaction-1",
        as_of=NOW + timedelta(seconds=10),
    )

    assert expired.state is BrowserFlowState.EXPIRED
    assert [event.state for event in store.pending_outbox()] == [
        BrowserFlowState.REGISTERED,
        BrowserFlowState.EXPIRED,
    ]
    with pytest.raises(BrowserFlowStateError):
        store.begin_citation(
            registered.opaque_token,
            auth_transaction_id="oidc-transaction-2",
            as_of=NOW + timedelta(seconds=11),
        )


def test_explicit_expiry_requires_deadline(tmp_path: Path) -> None:
    store = _store(tmp_path)
    registered = _register(store)

    with pytest.raises(BrowserFlowNotExpiredError):
        store.expire(registered.record.record_id, as_of=NOW + timedelta(seconds=59))

    expired = store.expire(registered.record.record_id, as_of=NOW + timedelta(seconds=60))
    assert expired.state is BrowserFlowState.EXPIRED


def test_store_survives_process_style_restart(tmp_path: Path) -> None:
    path = tmp_path / "browser-flows.sqlite3"
    first_store = LocalSQLiteBrowserFlowStore(path)
    registered = _register(first_store)
    first_store.begin_citation(
        registered.opaque_token,
        auth_transaction_id="oidc-transaction-1",
        as_of=NOW + timedelta(seconds=1),
    )

    restarted = LocalSQLiteBrowserFlowStore(path)

    assert restarted.get(registered.record.record_id).state is BrowserFlowState.AUTH_PENDING
    assert len(restarted.pending_outbox()) == 2


def test_concurrent_starts_allow_exactly_one_transition(tmp_path: Path) -> None:
    store = _store(tmp_path)
    registered = _register(store)

    def attempt(index: int) -> str:
        try:
            record = store.begin_citation(
                registered.opaque_token,
                auth_transaction_id=f"oidc-transaction-{index}",
                as_of=NOW + timedelta(seconds=1),
            )
            return record.state.value
        except BrowserFlowStateError:
            return "REJECTED"

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(attempt, range(16)))

    assert outcomes.count("AUTH_PENDING") == 1
    assert outcomes.count("REJECTED") == 15
    assert len(store.pending_outbox()) == 2


def test_concurrent_callbacks_consume_exactly_once(tmp_path: Path) -> None:
    store = _store(tmp_path)
    registered = _register(store)
    pending = store.begin_citation(
        registered.opaque_token,
        auth_transaction_id="oidc-transaction-1",
        as_of=NOW + timedelta(seconds=1),
    )

    def attempt(_: int) -> str:
        try:
            record = store.consume_citation(
                pending.record_id,
                actor=_registration().expected_actor,
                tenant="demo-bank",
                installation_id="inst_demo_bank",
                auth_transaction_id="oidc-transaction-1",
                as_of=NOW + timedelta(seconds=2),
            )
            return record.state.value
        except BrowserFlowStateError:
            return "REJECTED"

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(attempt, range(16)))

    assert outcomes.count("CONSUMED") == 1
    assert outcomes.count("REJECTED") == 15
    assert len(store.pending_outbox()) == 3


def test_outbox_acknowledgement_is_durable_and_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "browser-flows.sqlite3"
    store = LocalSQLiteBrowserFlowStore(path)
    _register(store)
    event = store.pending_outbox()[0]
    first_time = NOW + timedelta(seconds=5)

    first = store.mark_outbox_delivered(event.event_id, delivered_at=first_time)
    retried = store.mark_outbox_delivered(event.event_id, delivered_at=NOW + timedelta(seconds=10))
    restarted = LocalSQLiteBrowserFlowStore(path)

    assert first.delivered_at == first_time
    assert retried.delivered_at == first_time
    assert restarted.pending_outbox() == ()
    with pytest.raises(BrowserFlowOutboxError):
        store.mark_outbox_delivered("missing-event", delivered_at=first_time)


def test_store_rejects_unsafe_lifetime_and_runtime_shape(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with pytest.raises(ValueError, match="at most 60 seconds"):
        _register(store, lifetime=timedelta(seconds=61))
    assert store.pending_outbox() == ()
    with pytest.raises(ValueError, match="production"):
        LocalSQLiteBrowserFlowStore(tmp_path / "prod.sqlite3", production=True)
    with pytest.raises(ValueError, match="exactly one replica"):
        LocalSQLiteBrowserFlowStore(tmp_path / "replicas.sqlite3", replica_count=2)


def _grant_registration() -> GrantFlowRegistration:
    return GrantFlowRegistration(
        installation_id="inst_demo_bank",
        tenant="demo-bank",
        protocol_version="1",
        pkce_challenge=pkce_s256(PKCE_VERIFIER),
        correlation_id="grant-corr-001",
    )


def _grant_authorization(
    *,
    installation_id: str = "inst_demo_bank",
    tenant: str = "demo-bank",
    subject_lifetime: timedelta = timedelta(seconds=90),
) -> GrantAuthorization:
    return GrantAuthorization(
        installation_id=installation_id,
        client_id="demo-bank-portal-bff",
        source_issuer="https://idp.demo-bank.example",
        source_subject="fictional-subject-123",
        tenant=tenant,
        scopes=("cdd.read", "documents.read"),
        subject_expires_at=NOW + subject_lifetime,
    )


def _register_grant(
    store: LocalSQLiteBrowserFlowStore,
    *,
    lifetime: timedelta = timedelta(seconds=120),
):
    return store.register_grant(
        _grant_registration(),
        now=NOW,
        expires_at=NOW + lifetime,
    )


def _authorize_grant(
    store: LocalSQLiteBrowserFlowStore,
    opaque_instance_id: str,
    *,
    as_of: datetime = NOW + timedelta(seconds=1),
    authorization: GrantAuthorization | None = None,
):
    return store.authorize_grant(
        opaque_instance_id,
        authorization or _grant_authorization(),
        as_of=as_of,
    )


def test_pure_grant_state_machine_is_closed_and_subject_bounded() -> None:
    registered = new_grant_flow(
        record_id="grant-flow-001",
        instance_hash=hash_opaque_token("opaque-instance"),
        registration=_grant_registration(),
        created_at=NOW,
        expires_at=NOW + timedelta(seconds=120),
    )
    authorization = _grant_authorization(subject_lifetime=timedelta(seconds=30))

    issued = authorize_grant_flow(
        registered,
        authorization,
        code_hash=hash_opaque_token("launch-code"),
        as_of=NOW + timedelta(seconds=1),
    )
    repeated = authorize_grant_flow(
        registered,
        authorization,
        code_hash=hash_opaque_token("launch-code"),
        as_of=NOW + timedelta(seconds=1),
    )
    consumed = transition_grant_flow(
        issued,
        BrowserFlowState.CONSUMED,
        as_of=NOW + timedelta(seconds=2),
    )

    assert issued == repeated
    assert issued.code_expires_at == NOW + timedelta(seconds=30)
    assert consumed.state is BrowserFlowState.CONSUMED
    with pytest.raises(BrowserFlowStateError):
        transition_grant_flow(
            registered,
            BrowserFlowState.CONSUMED,
            as_of=NOW + timedelta(seconds=1),
        )


def test_grant_registration_validates_pkce_and_lifetime(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with pytest.raises(ValueError, match="S256"):
        GrantFlowRegistration(
            installation_id="inst_demo_bank",
            tenant="demo-bank",
            protocol_version="1",
            pkce_challenge=pkce_s256(PKCE_VERIFIER),
            pkce_method="plain",
            correlation_id="grant-corr-001",
        )
    with pytest.raises(ValueError, match="at most 120 seconds"):
        _register_grant(store, lifetime=timedelta(seconds=121))
    assert store.pending_outbox() == ()


def test_grant_persists_only_instance_and_code_hashes(tmp_path: Path) -> None:
    path = tmp_path / "browser-flows.sqlite3"
    store = LocalSQLiteBrowserFlowStore(path)
    registered = _register_grant(store)
    issued = _authorize_grant(store, registered.opaque_instance_id)

    with sqlite3.connect(path) as connection:
        row = connection.execute(
            """
            SELECT flow_kind, ticket_hash, code_hash, pkce_challenge
            FROM browser_flows WHERE record_id = ?
            """,
            (registered.record.record_id,),
        ).fetchone()
    assert row == (
        BrowserFlowKind.EMBEDDED_GRANT.value,
        hash_opaque_token(registered.opaque_instance_id),
        hash_opaque_token(issued.launch_code),
        pkce_s256(PKCE_VERIFIER),
    )
    database_text = path.read_bytes().decode("utf-8", errors="ignore")
    assert registered.opaque_instance_id not in database_text
    assert issued.launch_code not in database_text
    assert PKCE_VERIFIER not in database_text
    assert len(registered.opaque_instance_id) >= 43
    assert len(issued.launch_code) >= 43
    assert registered.opaque_instance_id not in repr(registered)
    assert issued.launch_code not in repr(issued)


@pytest.mark.parametrize(
    "authorization",
    [
        _grant_authorization(installation_id="inst_other"),
        _grant_authorization(tenant="other-bank"),
    ],
)
def test_grant_authorization_requires_exact_installation_and_tenant(
    tmp_path: Path, authorization: GrantAuthorization
) -> None:
    store = _store(tmp_path)
    registered = _register_grant(store)

    with pytest.raises(BrowserFlowBindingError):
        _authorize_grant(
            store,
            registered.opaque_instance_id,
            authorization=authorization,
        )

    record = store.get(registered.record.record_id)
    assert isinstance(record, EmbeddedGrantRecord)
    assert record.state is BrowserFlowState.REGISTERED
    assert len(store.pending_outbox()) == 1


@pytest.mark.parametrize(
    ("launch_code", "verifier", "installation_id"),
    [
        ("wrong-launch-code", PKCE_VERIFIER, "inst_demo_bank"),
        ("correct", "B" * 43, "inst_demo_bank"),
        ("correct", PKCE_VERIFIER, "inst_other"),
    ],
)
def test_grant_consume_rejects_code_pkce_or_installation_mismatch(
    tmp_path: Path,
    launch_code: str,
    verifier: str,
    installation_id: str,
) -> None:
    store = _store(tmp_path)
    registered = _register_grant(store)
    issued = _authorize_grant(store, registered.opaque_instance_id)
    presented_code = issued.launch_code if launch_code == "correct" else launch_code

    with pytest.raises(BrowserFlowBindingError):
        store.consume_grant(
            registered.opaque_instance_id,
            presented_code,
            verifier,
            installation_id=installation_id,
            as_of=NOW + timedelta(seconds=2),
        )

    assert store.get(registered.record.record_id).state is BrowserFlowState.CODE_ISSUED
    assert len(store.pending_outbox()) == 2


def test_grant_consumes_once_and_preserves_exact_authorization(tmp_path: Path) -> None:
    store = _store(tmp_path)
    registered = _register_grant(store)
    authorization = _grant_authorization()
    issued = _authorize_grant(store, registered.opaque_instance_id, authorization=authorization)

    consumed = store.consume_grant(
        registered.opaque_instance_id,
        issued.launch_code,
        PKCE_VERIFIER,
        installation_id="inst_demo_bank",
        as_of=NOW + timedelta(seconds=2),
    )

    assert consumed.state is BrowserFlowState.CONSUMED
    assert consumed.authorization == authorization
    with pytest.raises(BrowserFlowStateError):
        store.consume_grant(
            registered.opaque_instance_id,
            issued.launch_code,
            PKCE_VERIFIER,
            installation_id="inst_demo_bank",
            as_of=NOW + timedelta(seconds=3),
        )


def test_concurrent_grant_authorization_issues_exactly_one_code(tmp_path: Path) -> None:
    store = _store(tmp_path)
    registered = _register_grant(store)

    def attempt(_: int) -> str:
        try:
            result = _authorize_grant(store, registered.opaque_instance_id)
            return result.launch_code
        except BrowserFlowStateError:
            return "REJECTED"

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(attempt, range(16)))

    winners = [outcome for outcome in outcomes if outcome != "REJECTED"]
    assert len(winners) == 1
    assert outcomes.count("REJECTED") == 15
    assert len(store.pending_outbox()) == 2


def test_concurrent_grant_consume_has_exactly_one_winner(tmp_path: Path) -> None:
    store = _store(tmp_path)
    registered = _register_grant(store)
    issued = _authorize_grant(store, registered.opaque_instance_id)

    def attempt(_: int) -> str:
        try:
            result = store.consume_grant(
                registered.opaque_instance_id,
                issued.launch_code,
                PKCE_VERIFIER,
                installation_id="inst_demo_bank",
                as_of=NOW + timedelta(seconds=2),
            )
            return result.state.value
        except BrowserFlowStateError:
            return "REJECTED"

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(attempt, range(16)))

    assert outcomes.count("CONSUMED") == 1
    assert outcomes.count("REJECTED") == 15
    assert len(store.pending_outbox()) == 3


def test_grant_restart_preserves_authorization_and_single_use(tmp_path: Path) -> None:
    path = tmp_path / "browser-flows.sqlite3"
    first = LocalSQLiteBrowserFlowStore(path)
    registered = _register_grant(first)
    issued = _authorize_grant(first, registered.opaque_instance_id)

    restarted = LocalSQLiteBrowserFlowStore(path)
    record = restarted.get(registered.record.record_id)
    consumed = restarted.consume_grant(
        registered.opaque_instance_id,
        issued.launch_code,
        PKCE_VERIFIER,
        installation_id="inst_demo_bank",
        as_of=NOW + timedelta(seconds=2),
    )

    assert isinstance(record, EmbeddedGrantRecord)
    assert record.authorization == _grant_authorization()
    assert consumed.state is BrowserFlowState.CONSUMED


def test_store_migrates_existing_citation_schema_without_data_loss(tmp_path: Path) -> None:
    path = tmp_path / "browser-flows.sqlite3"
    record = new_citation_flow(
        record_id="existing-citation-flow",
        ticket_hash=hash_opaque_token("existing-ticket"),
        registration=_registration(),
        created_at=NOW,
        expires_at=NOW + timedelta(seconds=60),
    )
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE browser_flows (
                record_id TEXT PRIMARY KEY,
                flow_kind TEXT NOT NULL CHECK (flow_kind = 'citation_continuation'),
                ticket_hash TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL CHECK (
                    state IN ('REGISTERED', 'AUTH_PENDING', 'CONSUMED', 'EXPIRED')
                ),
                installation_id TEXT NOT NULL,
                tenant TEXT NOT NULL,
                source_actor TEXT NOT NULL,
                expected_actor TEXT NOT NULL,
                case_id TEXT NOT NULL,
                evidence_id TEXT NOT NULL,
                citation_id TEXT NOT NULL,
                correlation_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                state_changed_at TEXT NOT NULL,
                auth_transaction_id TEXT
            );
            CREATE TABLE browser_flow_outbox (
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
        )
        registration = record.registration
        connection.execute(
            """
            INSERT INTO browser_flows VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                record.record_id,
                record.kind.value,
                record.ticket_hash,
                record.state.value,
                registration.installation_id,
                registration.tenant,
                registration.source_actor,
                registration.expected_actor,
                registration.case_id,
                registration.evidence_id,
                registration.citation_id,
                registration.correlation_id,
                record.created_at.isoformat(timespec="microseconds"),
                record.expires_at.isoformat(timespec="microseconds"),
                record.state_changed_at.isoformat(timespec="microseconds"),
                None,
            ),
        )
        connection.execute(
            """
            INSERT INTO browser_flow_outbox VALUES (
                'existing-event', ?, 'citation_continuation', 'REGISTERED',
                'inst_demo_bank', 'demo-bank', 'corr-001', ?, NULL
            )
            """,
            (record.record_id, record.created_at.isoformat(timespec="microseconds")),
        )

    migrated = LocalSQLiteBrowserFlowStore(path)
    grant = _register_grant(migrated)

    assert migrated.get(record.record_id) == record
    assert isinstance(migrated.get(grant.record.record_id), EmbeddedGrantRecord)
    assert {event.event_id for event in migrated.pending_outbox()} >= {"existing-event"}


def test_expired_registration_and_code_commit_expiry_outbox(tmp_path: Path) -> None:
    store = _store(tmp_path)
    expired_registration = _register_grant(store, lifetime=timedelta(seconds=10))

    with pytest.raises(BrowserFlowExpiredError):
        _authorize_grant(
            store,
            expired_registration.opaque_instance_id,
            as_of=NOW + timedelta(seconds=10),
        )
    assert store.get(expired_registration.record.record_id).state is BrowserFlowState.EXPIRED

    active = _register_grant(store)
    issued = _authorize_grant(
        store,
        active.opaque_instance_id,
        authorization=_grant_authorization(subject_lifetime=timedelta(seconds=20)),
    )
    assert issued.record.code_expires_at == NOW + timedelta(seconds=20)
    with pytest.raises(BrowserFlowExpiredError):
        store.consume_grant(
            active.opaque_instance_id,
            issued.launch_code,
            PKCE_VERIFIER,
            installation_id="inst_demo_bank",
            as_of=NOW + timedelta(seconds=20),
        )
    assert store.get(active.record.record_id).state is BrowserFlowState.EXPIRED
    states = [event.state for event in store.pending_outbox()]
    assert states.count(BrowserFlowState.REGISTERED) == 2
    assert states.count(BrowserFlowState.CODE_ISSUED) == 1
    assert states.count(BrowserFlowState.EXPIRED) == 2


def test_citation_and_grant_tokens_cannot_cross_flow_kinds(tmp_path: Path) -> None:
    store = _store(tmp_path)
    citation = _register(store)
    grant = _register_grant(store)

    with pytest.raises(BrowserFlowNotFoundError):
        store.begin_citation(
            grant.opaque_instance_id,
            auth_transaction_id="oidc-transaction-1",
            as_of=NOW + timedelta(seconds=1),
        )
    with pytest.raises(BrowserFlowNotFoundError):
        _authorize_grant(store, citation.opaque_token)


def test_grant_outbox_is_sanitized_and_idempotently_addressed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    registered = _register_grant(store)
    issued = _authorize_grant(store, registered.opaque_instance_id)
    events = store.pending_outbox()

    assert [event.flow_kind for event in events] == [
        BrowserFlowKind.EMBEDDED_GRANT,
        BrowserFlowKind.EMBEDDED_GRANT,
    ]
    assert len({event.event_id for event in events}) == 2
    for event in events:
        serialized = repr(event)
        assert issued.launch_code not in serialized
        assert PKCE_VERIFIER not in serialized
        assert _grant_authorization().source_subject not in serialized
        assert not hasattr(event, "client_id")
        assert not hasattr(event, "scopes")
