from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cdd_sow_research.adapters.gcp.firestore_browser_flow_store import (
    FirestoreBrowserFlowStoreAdapter,
    FirestoreClientAssertionReplayStore,
    _event_document,
    _event_from_document,
    _record_document,
    _record_from_document,
)
from cdd_sow_research.adapters.gcp.firestore_embed_rate_limiter import (
    FirestoreFixedWindowRateLimiter,
    _next_rate_limit,
)
from cdd_sow_research.api.embed import RateLimitExceeded
from cdd_sow_research.config import Settings
from cdd_sow_research.domain.browser_flow import (
    BrowserFlowBindingError,
    BrowserFlowNotFoundError,
    BrowserFlowState,
    BrowserFlowStateError,
    CitationLedgerEntry,
    GrantAuthorization,
    GrantFlowRegistration,
    authorize_grant_flow,
    browser_flow_event,
    hash_opaque_token,
    new_grant_flow,
    pkce_s256,
    transition_grant_flow,
)
from cdd_sow_research.domain.identity import IdentityError

NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)


class _Snapshot:
    def __init__(self, value):
        self._value = value
        self.exists = value is not None

    def to_dict(self):
        return dict(self._value)


class _Reference:
    def __init__(self, memory, collection, document):
        self._memory = memory
        self._key = (collection, document)

    def get(self, *, transaction=None):
        del transaction
        return _Snapshot(self._memory.get(self._key))


class _Collection:
    def __init__(self, memory, name):
        self._memory = memory
        self._name = name

    def document(self, document):
        return _Reference(self._memory, self._name, document)


class _Transaction:
    def __init__(self, memory):
        self._memory = memory

    def create(self, reference, value):
        if reference._key in self._memory:
            raise AssertionError("unexpected duplicate create")
        self._memory[reference._key] = dict(value)

    def update(self, reference, value):
        self._memory[reference._key] = {
            **self._memory[reference._key],
            **dict(value),
        }


class _MemoryMixin:
    def _memory_init(self):
        self._memory = {}

    def _collection(self, configured_name):
        return _Collection(self._memory, configured_name)

    def _transaction(self, callback):
        return callback(_Transaction(self._memory))


class _MemoryFlowStore(_MemoryMixin, FirestoreBrowserFlowStoreAdapter):
    def __init__(self, settings):
        FirestoreBrowserFlowStoreAdapter.__init__(self, settings)
        self._memory_init()


class _MemoryReplayStore(_MemoryMixin, FirestoreClientAssertionReplayStore):
    def __init__(self, settings):
        FirestoreClientAssertionReplayStore.__init__(self, settings)
        self._memory_init()


def _consumed_grant():
    registered = new_grant_flow(
        record_id="managed-record",
        instance_hash=hash_opaque_token("managed-instance"),
        registration=GrantFlowRegistration(
            installation_id="inst_demo_bank",
            tenant="demo-bank",
            protocol_version="1",
            pkce_challenge="A" * 43,
            correlation_id="managed-correlation",
        ),
        created_at=NOW,
        expires_at=NOW + timedelta(seconds=120),
    )
    issued = authorize_grant_flow(
        registered,
        GrantAuthorization(
            installation_id="inst_demo_bank",
            client_id="demo-bank-bff",
            source_issuer="https://idp.demo-bank.example",
            source_subject="fictional-subject",
            tenant="demo-bank",
            scopes=("cdd.read",),
            subject_expires_at=NOW + timedelta(seconds=120),
        ),
        code_hash=hash_opaque_token("managed-code"),
        as_of=NOW + timedelta(seconds=1),
    )
    return transition_grant_flow(
        issued,
        BrowserFlowState.CONSUMED,
        as_of=NOW + timedelta(seconds=2),
    )


def test_managed_browser_flow_document_round_trip_preserves_binding() -> None:
    record = _consumed_grant()

    restored = _record_from_document(_record_document(record))

    assert restored == record
    assert "fictional-subject" in _record_document(record)["authorization"]["source_subject"]
    assert "managed-instance" not in repr(_record_document(record))
    assert "managed-code" not in repr(_record_document(record))


def test_managed_outbox_document_is_sanitized_and_round_trips() -> None:
    event = browser_flow_event(_consumed_grant())

    stored = _event_document(event)

    assert _event_from_document(stored) == event
    assert set(stored) == {
        "event_id",
        "record_id",
        "flow_kind",
        "state",
        "installation_id",
        "tenant",
        "correlation_id",
        "occurred_at",
        "delivered_at",
    }


def test_gcp_profile_selects_regional_managed_browser_flow_store() -> None:
    settings = Settings.load("config/settings.yaml")

    binding = settings.adapters["browser_flow_store"]["gcp"]

    assert binding.endswith("gcp.firestore_browser_flow_store:FirestoreBrowserFlowStoreAdapter")
    assert settings.browser_flow_store.database == "sow-cases"


def test_managed_rate_limiter_is_lazy_and_resets_only_after_window() -> None:
    settings = Settings.load("config/settings.yaml")
    limiter = FirestoreFixedWindowRateLimiter(settings, max_attempts=2, window_seconds=60)

    assert limiter._client is None
    first = _next_rate_limit(
        {},
        operation="grant",
        checked_at=NOW,
        window=timedelta(seconds=60),
        max_attempts=2,
    )
    second = _next_rate_limit(
        first,
        operation="grant",
        checked_at=NOW + timedelta(seconds=1),
        window=timedelta(seconds=60),
        max_attempts=2,
    )
    assert second["count"] == 2
    with pytest.raises(RateLimitExceeded):
        _next_rate_limit(
            second,
            operation="grant",
            checked_at=NOW + timedelta(seconds=59),
            window=timedelta(seconds=60),
            max_attempts=2,
        )
    reset = _next_rate_limit(
        second,
        operation="grant",
        checked_at=NOW + timedelta(seconds=60),
        window=timedelta(seconds=60),
        max_attempts=2,
    )
    assert reset["count"] == 1


def test_managed_citation_batch_conflict_writes_nothing() -> None:
    store = _MemoryFlowStore(Settings.load("config/settings.yaml"))
    conflicting = CitationLedgerEntry(
        citation_id="existing",
        tenant="demo-bank",
        source_actor="issub:actor",
        case_id="case",
        evidence_id="existing-evidence",
        source_id="existing-source",
        page=1,
    )
    store.record_citations((conflicting,))
    new_entry = CitationLedgerEntry(
        citation_id="new",
        tenant="demo-bank",
        source_actor="issub:actor",
        case_id="case",
        evidence_id="new-evidence",
        source_id="new-source",
        page=2,
    )
    rebound = CitationLedgerEntry(
        citation_id=conflicting.citation_id,
        tenant=conflicting.tenant,
        source_actor=conflicting.source_actor,
        case_id=conflicting.case_id,
        evidence_id="different",
        source_id="different",
        page=conflicting.page,
    )

    with pytest.raises(BrowserFlowBindingError):
        store.record_citations((new_entry, rebound))
    with pytest.raises(BrowserFlowNotFoundError):
        store.get_citation(
            new_entry.citation_id,
            tenant=new_entry.tenant,
            source_actor=new_entry.source_actor,
        )


def test_managed_citation_batch_is_idempotent_with_duplicate_input() -> None:
    store = _MemoryFlowStore(Settings.load("config/settings.yaml"))
    entry = CitationLedgerEntry(
        citation_id="duplicate",
        tenant="demo-bank",
        source_actor="issub:actor",
        case_id="case",
        evidence_id="evidence",
        source_id="source",
        page=2,
    )

    store.record_citations((entry, entry))

    assert (
        store.get_citation(
            entry.citation_id,
            tenant=entry.tenant,
            source_actor=entry.source_actor,
        )
        == entry
    )


def test_managed_grant_store_issues_and_consumes_exactly_once() -> None:
    store = _MemoryFlowStore(Settings.load("config/settings.yaml"))
    verifier = "V" * 43
    registered = store.register_grant(
        GrantFlowRegistration(
            installation_id="inst_demo_bank",
            tenant="demo-bank",
            protocol_version="1",
            pkce_challenge=pkce_s256(verifier),
            correlation_id="managed-flow",
        ),
        now=NOW,
        expires_at=NOW + timedelta(seconds=120),
    )
    issued = store.authorize_grant(
        registered.opaque_instance_id,
        GrantAuthorization(
            installation_id="inst_demo_bank",
            client_id="demo-bank-bff",
            source_issuer="https://idp.demo-bank.example",
            source_subject="fictional-managed-subject",
            tenant="demo-bank",
            scopes=("cdd.read",),
            subject_expires_at=NOW + timedelta(seconds=120),
        ),
        as_of=NOW + timedelta(seconds=1),
    )

    consumed = store.consume_grant(
        registered.opaque_instance_id,
        issued.launch_code,
        verifier,
        installation_id="inst_demo_bank",
        as_of=NOW + timedelta(seconds=2),
    )

    assert consumed.state is BrowserFlowState.CONSUMED
    assert store.get(consumed.record_id) == consumed
    with pytest.raises(BrowserFlowStateError, match="CODE_ISSUED"):
        store.consume_grant(
            registered.opaque_instance_id,
            issued.launch_code,
            verifier,
            installation_id="inst_demo_bank",
            as_of=NOW + timedelta(seconds=3),
        )


def test_managed_jti_replay_is_atomic_and_allows_only_expired_reuse() -> None:
    store = _MemoryReplayStore(Settings.load("config/settings.yaml"))
    jti = "J" * 22

    digest = store.consume(
        jti=jti,
        client_id="demo-bank-bff",
        expires_at=NOW + timedelta(seconds=60),
        as_of=NOW,
    )

    assert len(digest) == 64
    with pytest.raises(IdentityError, match="already been used"):
        store.consume(
            jti=jti,
            client_id="demo-bank-bff",
            expires_at=NOW + timedelta(seconds=60),
            as_of=NOW + timedelta(seconds=1),
        )
    assert (
        store.consume(
            jti=jti,
            client_id="demo-bank-bff",
            expires_at=NOW + timedelta(seconds=121),
            as_of=NOW + timedelta(seconds=61),
        )
        == digest
    )
