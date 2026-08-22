"""Deterministic state model for short-lived browser continuations."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final

# RFC 8693 subject-token types the broker can be configured to accept. Which one an
# installation actually accepts is reviewed per-installation configuration, never a
# request-selected value: see ``BrokerInstallationPolicy.subject_token_type``.
ACCESS_TOKEN_SUBJECT_TYPE: Final = "urn:ietf:params:oauth:token-type:access_token"
ID_TOKEN_SUBJECT_TYPE: Final = "urn:ietf:params:oauth:token-type:id_token"
SUBJECT_TOKEN_TYPES: Final = (ACCESS_TOKEN_SUBJECT_TYPE, ID_TOKEN_SUBJECT_TYPE)

MAX_CITATION_LIFETIME: Final = timedelta(seconds=60)
MAX_GRANT_REGISTRATION_LIFETIME: Final = timedelta(seconds=120)
MAX_GRANT_CODE_LIFETIME: Final = timedelta(seconds=60)
_PKCE_CHALLENGE: Final = re.compile(r"^[A-Za-z0-9_-]{43}$")
_PKCE_VERIFIER: Final = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")


class BrowserFlowError(RuntimeError):
    """Base error for a rejected browser-flow operation."""


class BrowserFlowNotFoundError(BrowserFlowError):
    """No flow exists for the supplied opaque reference."""


class BrowserFlowStateError(BrowserFlowError):
    """The requested state transition is not allowed."""


class BrowserFlowExpiredError(BrowserFlowError):
    """The flow expired before the requested operation."""


class BrowserFlowBindingError(BrowserFlowError):
    """The callback identity or transaction does not match the flow."""


class BrowserFlowNotExpiredError(BrowserFlowError):
    """Explicit expiry was attempted before the deadline."""


class BrowserFlowOutboxError(BrowserFlowError):
    """An outbox acknowledgement is invalid."""


class BrowserFlowKind(StrEnum):
    CITATION_CONTINUATION = "citation_continuation"
    EMBEDDED_GRANT = "embedded_grant"


class BrowserFlowState(StrEnum):
    REGISTERED = "REGISTERED"
    AUTH_PENDING = "AUTH_PENDING"
    CODE_ISSUED = "CODE_ISSUED"
    CONSUMED = "CONSUMED"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True, slots=True)
class CitationLedgerEntry:
    """One citation actually emitted to one verified embedded actor."""

    citation_id: str
    tenant: str
    source_actor: str
    case_id: str
    evidence_id: str
    source_id: str
    page: int | None

    def __post_init__(self) -> None:
        for field_name in (
            "citation_id",
            "tenant",
            "source_actor",
            "case_id",
            "evidence_id",
            "source_id",
        ):
            _safe_binding(getattr(self, field_name), field_name)
        if self.page is not None and (
            not isinstance(self.page, int) or isinstance(self.page, bool) or self.page < 1
        ):
            raise ValueError("citation ledger page must be a positive integer")


@dataclass(frozen=True, slots=True)
class CitationFlowRegistration:
    """Server-authorized binding used to mint one opaque citation ticket."""

    installation_id: str
    tenant: str
    source_actor: str
    expected_actor: str
    case_id: str
    evidence_id: str
    citation_id: str
    correlation_id: str

    def __post_init__(self) -> None:
        for field_name in (
            "installation_id",
            "tenant",
            "source_actor",
            "expected_actor",
            "case_id",
            "evidence_id",
            "citation_id",
            "correlation_id",
        ):
            _safe_binding(getattr(self, field_name), field_name)


@dataclass(frozen=True, slots=True)
class CitationContinuationRecord:
    """Persisted citation flow; it never contains the plaintext opaque ticket."""

    record_id: str
    ticket_hash: str
    state: BrowserFlowState
    registration: CitationFlowRegistration
    created_at: datetime
    expires_at: datetime
    state_changed_at: datetime
    auth_transaction_id: str | None = None
    kind: BrowserFlowKind = BrowserFlowKind.CITATION_CONTINUATION

    def __post_init__(self) -> None:
        _safe_binding(self.record_id, "record_id")
        if len(self.ticket_hash) != 64 or any(
            character not in "0123456789abcdef" for character in self.ticket_hash
        ):
            raise ValueError("ticket_hash must be a lowercase SHA-256 digest")
        if self.kind is not BrowserFlowKind.CITATION_CONTINUATION:
            raise ValueError("citation record must use citation_continuation flow kind")
        created_at = _aware_utc(self.created_at, "created_at")
        expires_at = _aware_utc(self.expires_at, "expires_at")
        changed_at = _aware_utc(self.state_changed_at, "state_changed_at")
        if changed_at < created_at:
            raise ValueError("state_changed_at must not predate creation")
        lifetime = expires_at - created_at
        if lifetime <= timedelta(0) or lifetime > MAX_CITATION_LIFETIME:
            raise ValueError("citation flow lifetime must be greater than 0 and at most 60 seconds")
        if self.state in {BrowserFlowState.CODE_ISSUED}:
            raise ValueError("citation records cannot use grant-only states")
        if self.state in {BrowserFlowState.AUTH_PENDING, BrowserFlowState.CONSUMED}:
            _safe_binding(self.auth_transaction_id, "auth_transaction_id")
        elif self.auth_transaction_id is not None:
            raise ValueError("auth_transaction_id is valid only after citation start")


@dataclass(frozen=True, slots=True)
class RegisteredBrowserFlow:
    """One-time registration result.

    ``opaque_token`` is returned only at creation.  It is never part of the stored
    record and cannot be recovered through the store.
    """

    record: CitationContinuationRecord
    opaque_token: str = field(repr=False)

    def __post_init__(self) -> None:
        _safe_binding(self.opaque_token, "opaque_token")


@dataclass(frozen=True, slots=True)
class GrantFlowRegistration:
    """Iframe-owned PKCE registration before any host authorization."""

    installation_id: str
    tenant: str
    protocol_version: str
    pkce_challenge: str
    correlation_id: str
    pkce_method: str = "S256"

    def __post_init__(self) -> None:
        for field_name in ("installation_id", "tenant", "protocol_version", "correlation_id"):
            _safe_binding(getattr(self, field_name), field_name)
        if self.pkce_method != "S256":
            raise ValueError("pkce_method must be exactly S256")
        if _PKCE_CHALLENGE.fullmatch(self.pkce_challenge) is None:
            raise ValueError("pkce_challenge must be an unpadded SHA-256 base64url value")


@dataclass(frozen=True, slots=True)
class GrantAuthorization:
    """Verified BFF and subject-credential binding for one instance."""

    installation_id: str
    client_id: str
    source_issuer: str
    source_subject: str
    tenant: str
    scopes: tuple[str, ...]
    subject_expires_at: datetime

    def __post_init__(self) -> None:
        for field_name in (
            "installation_id",
            "client_id",
            "source_issuer",
            "source_subject",
            "tenant",
        ):
            _safe_binding(getattr(self, field_name), field_name)
        if not self.scopes:
            raise ValueError("scopes must be non-empty")
        for scope in self.scopes:
            _safe_binding(scope, "scope")
        if len(set(self.scopes)) != len(self.scopes):
            raise ValueError("scopes must not contain duplicates")
        _aware_utc(self.subject_expires_at, "subject_expires_at")


@dataclass(frozen=True, slots=True)
class EmbeddedGrantRecord:
    """Persisted Mode 5 state; instance IDs, codes, and verifiers are absent."""

    record_id: str
    instance_hash: str
    state: BrowserFlowState
    registration: GrantFlowRegistration
    created_at: datetime
    expires_at: datetime
    state_changed_at: datetime
    authorization: GrantAuthorization | None = None
    code_hash: str | None = None
    code_issued_at: datetime | None = None
    code_expires_at: datetime | None = None
    kind: BrowserFlowKind = BrowserFlowKind.EMBEDDED_GRANT

    def __post_init__(self) -> None:
        _safe_binding(self.record_id, "record_id")
        _sha256_digest(self.instance_hash, "instance_hash")
        if self.kind is not BrowserFlowKind.EMBEDDED_GRANT:
            raise ValueError("grant record must use embedded_grant flow kind")
        created_at = _aware_utc(self.created_at, "created_at")
        expires_at = _aware_utc(self.expires_at, "expires_at")
        changed_at = _aware_utc(self.state_changed_at, "state_changed_at")
        if changed_at < created_at:
            raise ValueError("state_changed_at must not predate creation")
        lifetime = expires_at - created_at
        if lifetime <= timedelta(0) or lifetime > MAX_GRANT_REGISTRATION_LIFETIME:
            raise ValueError(
                "grant registration lifetime must be greater than 0 and at most 120 seconds"
            )
        if self.state is BrowserFlowState.AUTH_PENDING:
            raise ValueError("grant records cannot use citation-only states")
        code_states = {BrowserFlowState.CODE_ISSUED, BrowserFlowState.CONSUMED}
        has_code_fields = (
            self.authorization is not None
            and self.code_hash is not None
            and self.code_issued_at is not None
            and self.code_expires_at is not None
        )
        if self.state in code_states and not has_code_fields:
            raise ValueError("authorized grant states require complete code binding")
        if self.state is BrowserFlowState.REGISTERED and has_code_fields:
            raise ValueError("registered grant must not contain authorization or code state")
        if (
            any(
                value is not None
                for value in (
                    self.authorization,
                    self.code_hash,
                    self.code_issued_at,
                    self.code_expires_at,
                )
            )
            and not has_code_fields
        ):
            raise ValueError("grant authorization and code fields are all-or-none")
        if has_code_fields:
            assert self.authorization is not None
            assert self.code_hash is not None
            assert self.code_issued_at is not None
            assert self.code_expires_at is not None
            _sha256_digest(self.code_hash, "code_hash")
            issued_at = _aware_utc(self.code_issued_at, "code_issued_at")
            code_expires_at = _aware_utc(self.code_expires_at, "code_expires_at")
            subject_expires_at = _aware_utc(
                self.authorization.subject_expires_at, "subject_expires_at"
            )
            if issued_at < created_at or code_expires_at <= issued_at:
                raise ValueError("grant code times are invalid")
            if code_expires_at - issued_at > MAX_GRANT_CODE_LIFETIME:
                raise ValueError("grant code lifetime must be at most 60 seconds")
            if code_expires_at > min(expires_at, subject_expires_at):
                raise ValueError("grant code must not outlive registration or subject")


@dataclass(frozen=True, slots=True)
class RegisteredGrantInstance:
    """One-time grant registration result; the opaque instance is not stored."""

    record: EmbeddedGrantRecord
    opaque_instance_id: str = field(repr=False)

    def __post_init__(self) -> None:
        _safe_binding(self.opaque_instance_id, "opaque_instance_id")


@dataclass(frozen=True, slots=True)
class IssuedGrantCode:
    """One-time authorization result; the launch code cannot be recovered."""

    record: EmbeddedGrantRecord
    launch_code: str = field(repr=False)

    def __post_init__(self) -> None:
        _safe_binding(self.launch_code, "launch_code")
        if self.record.state is not BrowserFlowState.CODE_ISSUED:
            raise ValueError("issued grant result requires CODE_ISSUED state")


BrowserFlowRecord = CitationContinuationRecord | EmbeddedGrantRecord


@dataclass(frozen=True, slots=True)
class BrowserFlowOutboxEvent:
    """Sanitized, idempotently addressable transition event."""

    event_id: str
    record_id: str
    flow_kind: BrowserFlowKind
    state: BrowserFlowState
    installation_id: str
    tenant: str
    correlation_id: str
    occurred_at: datetime
    delivered_at: datetime | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "event_id",
            "record_id",
            "installation_id",
            "tenant",
            "correlation_id",
        ):
            _safe_binding(getattr(self, field_name), field_name)
        occurred_at = _aware_utc(self.occurred_at, "occurred_at")
        if self.delivered_at is not None:
            delivered_at = _aware_utc(self.delivered_at, "delivered_at")
            if delivered_at < occurred_at:
                raise ValueError("delivered_at must not predate the event")


def new_citation_flow(
    *,
    record_id: str,
    ticket_hash: str,
    registration: CitationFlowRegistration,
    created_at: datetime,
    expires_at: datetime,
) -> CitationContinuationRecord:
    """Create a deterministic registered record from caller-supplied values."""

    created_at = _aware_utc(created_at, "created_at")
    expires_at = _aware_utc(expires_at, "expires_at")
    return CitationContinuationRecord(
        record_id=record_id,
        ticket_hash=ticket_hash,
        state=BrowserFlowState.REGISTERED,
        registration=registration,
        created_at=created_at,
        expires_at=expires_at,
        state_changed_at=created_at,
    )


def transition_citation_flow(
    record: CitationContinuationRecord,
    target: BrowserFlowState,
    *,
    as_of: datetime,
    auth_transaction_id: str | None = None,
) -> CitationContinuationRecord:
    """Apply one exact citation transition with no I/O, clock reads, or randomness."""

    as_of = _aware_utc(as_of, "as_of")
    if as_of < record.state_changed_at:
        raise BrowserFlowStateError("as_of predates the current persisted state")
    if record.state in {BrowserFlowState.CONSUMED, BrowserFlowState.EXPIRED}:
        raise BrowserFlowStateError(f"{record.state.value} is terminal")

    if target is BrowserFlowState.EXPIRED:
        if as_of < record.expires_at:
            raise BrowserFlowNotExpiredError("flow has not reached its expiry")
        return replace(record, state=target, state_changed_at=as_of)
    if as_of >= record.expires_at:
        raise BrowserFlowExpiredError("flow has expired")

    allowed = {
        BrowserFlowState.REGISTERED: BrowserFlowState.AUTH_PENDING,
        BrowserFlowState.AUTH_PENDING: BrowserFlowState.CONSUMED,
    }
    if allowed.get(record.state) is not target:
        raise BrowserFlowStateError(
            f"citation transition {record.state.value} -> {target.value} is not allowed"
        )

    if target is BrowserFlowState.AUTH_PENDING:
        _safe_binding(auth_transaction_id, "auth_transaction_id")
        return replace(
            record,
            state=target,
            state_changed_at=as_of,
            auth_transaction_id=auth_transaction_id,
        )
    if auth_transaction_id != record.auth_transaction_id:
        raise BrowserFlowBindingError("authentication transaction does not match")
    return replace(record, state=target, state_changed_at=as_of)


def new_grant_flow(
    *,
    record_id: str,
    instance_hash: str,
    registration: GrantFlowRegistration,
    created_at: datetime,
    expires_at: datetime,
) -> EmbeddedGrantRecord:
    """Create a deterministic REGISTERED grant record."""

    created_at = _aware_utc(created_at, "created_at")
    expires_at = _aware_utc(expires_at, "expires_at")
    return EmbeddedGrantRecord(
        record_id=record_id,
        instance_hash=instance_hash,
        state=BrowserFlowState.REGISTERED,
        registration=registration,
        created_at=created_at,
        expires_at=expires_at,
        state_changed_at=created_at,
    )


def authorize_grant_flow(
    record: EmbeddedGrantRecord,
    authorization: GrantAuthorization,
    *,
    code_hash: str,
    as_of: datetime,
) -> EmbeddedGrantRecord:
    """Bind verified BFF/subject facts and move REGISTERED to CODE_ISSUED."""

    as_of = _aware_utc(as_of, "as_of")
    _sha256_digest(code_hash, "code_hash")
    if record.state is not BrowserFlowState.REGISTERED:
        raise BrowserFlowStateError("grant authorization requires REGISTERED state")
    if as_of < record.state_changed_at:
        raise BrowserFlowStateError("as_of predates the current persisted state")
    if (
        authorization.installation_id != record.registration.installation_id
        or authorization.tenant != record.registration.tenant
    ):
        raise BrowserFlowBindingError("grant installation or tenant does not match")
    if as_of >= record.expires_at or as_of >= authorization.subject_expires_at:
        raise BrowserFlowExpiredError("grant registration or subject has expired")
    code_expires_at = min(
        as_of + MAX_GRANT_CODE_LIFETIME,
        record.expires_at,
        authorization.subject_expires_at,
    )
    return replace(
        record,
        state=BrowserFlowState.CODE_ISSUED,
        state_changed_at=as_of,
        authorization=authorization,
        code_hash=code_hash,
        code_issued_at=as_of,
        code_expires_at=code_expires_at,
    )


def transition_grant_flow(
    record: EmbeddedGrantRecord,
    target: BrowserFlowState,
    *,
    as_of: datetime,
) -> EmbeddedGrantRecord:
    """Apply an exact grant consume or expiry transition."""

    as_of = _aware_utc(as_of, "as_of")
    if as_of < record.state_changed_at:
        raise BrowserFlowStateError("as_of predates the current persisted state")
    if record.state in {BrowserFlowState.CONSUMED, BrowserFlowState.EXPIRED}:
        raise BrowserFlowStateError(f"{record.state.value} is terminal")
    effective_expiry = record.expires_at
    if record.code_expires_at is not None:
        effective_expiry = min(effective_expiry, record.code_expires_at)

    if target is BrowserFlowState.EXPIRED:
        if as_of < effective_expiry:
            raise BrowserFlowNotExpiredError("flow has not reached its expiry")
        return replace(record, state=target, state_changed_at=as_of)
    if as_of >= effective_expiry:
        raise BrowserFlowExpiredError("grant has expired")
    if record.state is not BrowserFlowState.CODE_ISSUED or target is not BrowserFlowState.CONSUMED:
        raise BrowserFlowStateError(
            f"grant transition {record.state.value} -> {target.value} is not allowed"
        )
    return replace(record, state=target, state_changed_at=as_of)


def browser_flow_event(
    record: BrowserFlowRecord,
) -> BrowserFlowOutboxEvent:
    """Build the stable, sanitized event for the record's current transition."""

    event_material = (
        f"browser-flow:v1:{record.record_id}:{record.kind.value}:{record.state.value}"
    ).encode()
    event_id = hashlib.sha256(event_material).hexdigest()
    return BrowserFlowOutboxEvent(
        event_id=event_id,
        record_id=record.record_id,
        flow_kind=record.kind,
        state=record.state,
        installation_id=record.registration.installation_id,
        tenant=record.registration.tenant,
        correlation_id=record.registration.correlation_id,
        occurred_at=record.state_changed_at,
    )


def hash_opaque_token(token: str) -> str:
    """Hash a presented ticket using the canonical storage representation."""

    _safe_binding(token, "opaque_token")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def pkce_s256(verifier: str) -> str:
    """Return the RFC 7636 S256 challenge for one valid verifier."""

    if not isinstance(verifier, str) or _PKCE_VERIFIER.fullmatch(verifier) is None:
        raise ValueError("pkce_verifier must be 43-128 RFC 7636 unreserved characters")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def pkce_verifier_matches(challenge: str, verifier: str) -> bool:
    """Compare a verifier-derived challenge in constant time."""

    if _PKCE_CHALLENGE.fullmatch(challenge) is None:
        return False
    try:
        derived = pkce_s256(verifier)
    except ValueError:
        return False
    return hmac.compare_digest(derived.encode("ascii"), challenge.encode("ascii"))


def _aware_utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _sha256_digest(value: object, field_name: str) -> str:
    text = _safe_binding(value, field_name)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return text


def _safe_binding(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError(f"{field_name} must be a non-empty bounded string")
    if any(ord(character) < 0x20 for character in value):
        raise ValueError(f"{field_name} must not contain control characters")
    return value
