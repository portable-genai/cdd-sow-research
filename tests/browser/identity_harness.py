"""Production-backed synthetic identity services for browser portability evidence.

The harness owns issuer keys and synthetic credentials, while cdd-sow-research's production adapters
own every verification and grant decision. Browser fixtures may receive a short-lived
Mode 4 access token and a Mode 5 launch code. They never receive the Mode 5 subject token,
BFF private-key assertion, or BFF private key.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import socket
import sqlite3
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import httpx
import jwt
import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from cdd_sow_research.adapters.local.browser_flow_store import LocalSQLiteBrowserFlowStore
from cdd_sow_research.adapters.oidc import jwks_verify
from cdd_sow_research.adapters.oidc.access_token_identity import (
    OAuthAccessTokenAuthenticationAdapter,
)
from cdd_sow_research.adapters.oidc.embed_token import (
    EmbedTokenIssuer,
    EmbedTokenKey,
    EmbedTokenPolicy,
    MintedEmbedToken,
)
from cdd_sow_research.adapters.oidc.embed_token_identity import (
    EmbeddedGrantTokenAuthenticationAdapter,
)
from cdd_sow_research.adapters.oidc.private_key_jwt import (
    PinnedClientKey,
    PrivateKeyJwtClientPolicy,
    PrivateKeyJwtVerifier,
    SQLiteClientAssertionReplayStore,
)
from cdd_sow_research.api.embed import (
    BffGrantClientPolicy,
    BrokerInstallationPolicy,
    EmbedBrokerDependencies,
    InMemoryFixedWindowRateLimiter,
    Rfc9068BrokerSubjectTokenVerifier,
    StaticBrokerInstallationResolver,
    create_embed_router,
)
from cdd_sow_research.api.security import OAuthAccessTokenApiAuthenticationAdapter
from cdd_sow_research.config import (
    AccessTokenIssuerSettings,
    ChannelSettings,
    Settings,
)
from cdd_sow_research.domain.browser_flow import BrowserFlowState, EmbeddedGrantRecord
from cdd_sow_research.domain.identity import IdentityError, RequestContext

TENANT = "demo-bank"
MODE4_RSA_INSTALLATION = "inst_host_a"
MODE4_EC_INSTALLATION = "inst_host_b"
MODE5_INSTALLATION = "inst_mode5"
MODE4_RSA_CLIENT = "portal-mode4-rsa"
MODE4_EC_CLIENT = "portal-mode4-ec"
MODE5_SUBJECT_CLIENT = "portal-mode5-subject"
MODE5_BFF_CLIENT = "portal-mode5-bff"
MODE4_SCOPES = ("cdd.read", "cdd.write", "documents.read")
MODE5_SCOPES = ("embed.grant", "cdd.read", "documents.read")
_BFF_SESSION_COOKIE = "cdd_harness_bff_session"


@dataclass(frozen=True, slots=True)
class _SigningKey:
    kid: str
    algorithm: Literal["RS256", "ES256"]
    private_key: Any
    public_jwk: dict[str, Any]
    private_pem: str
    public_pem: str


@dataclass(frozen=True, slots=True)
class Mode4Token:
    """One browser-permitted Mode 4 credential and its non-secret bindings."""

    access_token: str
    installation_id: str
    expires_at: int
    variant: str

    def __repr__(self) -> str:
        return (
            f"Mode4Token(installation_id={self.installation_id!r}, "
            f"expires_at={self.expires_at!r}, variant={self.variant!r})"
        )


class Mode4BrowserEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    json_call: bool
    form_data_call: bool
    blob_call: bool
    rsa_issuer: bool
    ec_issuer: bool
    rotation_refresh: bool
    cross_tenant_rejected: bool
    cross_installation_rejected: bool
    wrong_origin_rejected: bool
    wrong_type_rejected: bool
    credential_absent_from_dom: bool


class Mode5BrowserEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    iframe_registered_first: bool
    protected_call: bool
    wrong_verifier_rejected: bool
    launch_code_replay_rejected: bool
    sibling_origin_rejected: bool
    missing_csrf_rejected: bool
    wrong_csrf_rejected: bool
    subject_session_mismatch_rejected: bool
    instance_mismatch_rejected: bool
    duplicate_authorization_rejected: bool
    host_never_received_subject_token: bool
    host_never_received_pkce_verifier: bool
    host_never_received_doc1_token: bool
    credential_absent_from_dom: bool


class _Mode5AuthorizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    instance_id: str
    user_intent_id: str


class _Mode5IntentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    installation_id: Literal["inst_mode5"]
    instance_id: str
    action: Literal["authorize-embed"]


@dataclass(frozen=True, slots=True)
class _BffSession:
    session_digest: str
    csrf_digest: str
    source_subject: str
    expires_at: datetime


@dataclass(slots=True)
class _BffIntent:
    intent_id: str
    session_digest: str
    source_subject: str
    instance_id: str
    expires_at: datetime
    consumed: bool = False


class _Mode4ReportRequest(Mode4BrowserEvidence):
    pass


class _Mode5ReportRequest(Mode5BrowserEvidence):
    pass


class _BoundaryRecordingFlowStore(LocalSQLiteBrowserFlowStore):
    def __init__(
        self,
        database_path: Path,
        *,
        record_secret: Callable[[str, str], None],
    ) -> None:
        super().__init__(database_path)
        self._record_secret = record_secret

    def consume_grant(
        self,
        opaque_instance_id: str,
        launch_code: str,
        pkce_verifier: str,
        *,
        installation_id: str,
        as_of: datetime,
    ) -> EmbeddedGrantRecord:
        self._record_secret("mode5_pkce_verifier", pkce_verifier)
        return super().consume_grant(
            opaque_instance_id,
            launch_code,
            pkce_verifier,
            installation_id=installation_id,
            as_of=as_of,
        )


class _BoundaryRecordingEmbedTokenIssuer(EmbedTokenIssuer):
    def __init__(
        self,
        policy: EmbedTokenPolicy,
        *,
        environment: Callable[[str], str | None],
        record_secret: Callable[[str, str], None],
    ) -> None:
        super().__init__(policy, environment=environment)
        self._record_secret = record_secret

    def mint(self, record: EmbeddedGrantRecord, *, as_of: datetime) -> MintedEmbedToken:
        minted = super().mint(record, as_of=as_of)
        self._record_secret("mode5_doc1_token", minted.access_token)
        return minted


class _HarnessState:
    def __init__(
        self,
        root: Path,
        *,
        origin: str,
        agent_origin: str,
        mode5_agent_origin: str,
        host_origin: str,
    ) -> None:
        self.root = root
        self.origin = origin
        self.agent_origin = agent_origin
        self.mode5_agent_origin = mode5_agent_origin
        self.host_origin = host_origin
        self.lock = threading.Lock()
        self.rsa_old = _rsa_key("rsa-old")
        self.rsa_rotated = _rsa_key("rsa-rotated")
        self.ec_active = _ec_key("ec-active")
        self.bff_key = _ec_key("bff-ec")
        self.embed_key = _ec_key("doc1-embed-ec")
        self.rsa_rotated_active = False
        self.jwks_fetches = {"rsa": 0, "ec": 0}
        self.seen_mode4_kids: set[str] = set()
        self.mode4_negatives: set[str] = set()
        self.mode4_report: Mode4BrowserEvidence | None = None
        self.mode5_report: Mode5BrowserEvidence | None = None
        self.mode5_authorized = False
        self.mode5_protected = False
        self.bff_sessions: dict[str, _BffSession] = {}
        self.bff_intents: dict[str, _BffIntent] = {}
        self.bff_negatives: set[str] = set()
        self.sensitive_values: list[str] = []
        self.boundary_secret_hashes: dict[str, set[str]] = {}
        self.manifest_path = root / "mode4-installations.json"
        self.flow_path = root / "browser-flow.sqlite3"
        self.replay_path = root / "bff-replay.sqlite3"
        self.mode4_audience = f"{agent_origin}/agent/api"
        self.broker_audience = f"{mode5_agent_origin}/agent/api/v1/embed/grants"
        self.embed_audience = f"{mode5_agent_origin}/agent/api"
        self.rsa_issuer = f"{origin}/issuers/rsa"
        self.ec_issuer = f"{origin}/issuers/ec"
        self.rsa_jwks = f"{origin}/v1/harness/jwks/rsa"
        self.ec_jwks = f"{origin}/v1/harness/jwks/ec"
        self._write_mode4_manifest()
        self.mode4_adapter = OAuthAccessTokenApiAuthenticationAdapter(
            OAuthAccessTokenAuthenticationAdapter(self._mode4_settings())
        )
        self.flow_store = _BoundaryRecordingFlowStore(
            self.flow_path,
            record_secret=self._record_boundary_secret,
        )
        self.embed_issuer = _BoundaryRecordingEmbedTokenIssuer(
            EmbedTokenPolicy(
                issuer=f"{origin}/issuers/doc1-embed",
                audience=self.embed_audience,
                active_kid=self.embed_key.kid,
                keys=(
                    EmbedTokenKey(
                        kid=self.embed_key.kid,
                        algorithm=self.embed_key.algorithm,
                        private_key_env="HARNESS_EMBED_PRIVATE",
                        public_key_env="HARNESS_EMBED_PUBLIC",
                    ),
                ),
                lifetime_seconds=90,
                clock_skew_seconds=5,
            ),
            environment={
                "HARNESS_EMBED_PRIVATE": self.embed_key.private_pem,
                "HARNESS_EMBED_PUBLIC": self.embed_key.public_pem,
            }.get,
            record_secret=self._record_boundary_secret,
        )
        self.mode5_adapter = EmbeddedGrantTokenAuthenticationAdapter(
            self.embed_issuer,
            installation_ids=(MODE5_INSTALLATION,),
        )
        self.bff_verifier = PrivateKeyJwtVerifier(
            (
                PrivateKeyJwtClientPolicy(
                    client_id=MODE5_BFF_CLIENT,
                    audience=self.broker_audience,
                    keys=(
                        PinnedClientKey(
                            kid=self.bff_key.kid,
                            algorithm=self.bff_key.algorithm,
                            public_jwk=self.bff_key.public_jwk,
                        ),
                    ),
                    max_lifetime_seconds=60,
                    clock_skew_seconds=5,
                ),
            ),
            SQLiteClientAssertionReplayStore(self.replay_path),
        )
        self.broker_dependencies = EmbedBrokerDependencies(
            store=self.flow_store,
            installations=StaticBrokerInstallationResolver((self._mode5_installation(),)),
            subject_tokens=Rfc9068BrokerSubjectTokenVerifier(),
            bff_assertions=self.bff_verifier,
            token_issuer=self.embed_issuer,
            rate_limiter=InMemoryFixedWindowRateLimiter(max_attempts=100),
            clock=lambda: datetime.now(UTC),
        )

    @property
    def active_rsa(self) -> _SigningKey:
        return self.rsa_rotated if self.rsa_rotated_active else self.rsa_old

    def rotate_rsa(self) -> str:
        with self.lock:
            self.rsa_rotated_active = True
            return self.rsa_rotated.kid

    def jwks(self, family: Literal["rsa", "ec"]) -> dict[str, object]:
        with self.lock:
            self.jwks_fetches[family] += 1
            key = self.active_rsa if family == "rsa" else self.ec_active
            return {"keys": [key.public_jwk]}

    def mint_mode4(self, variant: str = "valid") -> Mode4Token:
        now = int(time.time())
        if variant == "refresh":
            self.rotate_rsa()
        key = self.ec_active if variant == "ec" else self.active_rsa
        installation_id = MODE4_EC_INSTALLATION if variant == "ec" else MODE4_RSA_INSTALLATION
        client_id = MODE4_EC_CLIENT if variant == "ec" else MODE4_RSA_CLIENT
        tenant = "other-bank" if variant == "cross-tenant" else TENANT
        signed_installation = (
            MODE4_EC_INSTALLATION if variant == "cross-installation" else installation_id
        )
        token_type = "JWT" if variant == "wrong-type" else "at+jwt"
        claims = {
            "iss": self.ec_issuer if variant == "ec" else self.rsa_issuer,
            "sub": "synthetic-analyst-001",
            "aud": self.mode4_audience,
            "iat": now - 1,
            "exp": now + 59,
            "jti": secrets.token_urlsafe(24),
            "client_id": client_id,
            "tenant": tenant,
            "scope": " ".join(MODE4_SCOPES),
            "installation_id": signed_installation,
            "groups": ["cdd-analyst"],
        }
        access_token = jwt.encode(
            claims,
            key.private_key,
            algorithm=key.algorithm,
            headers={"kid": key.kid, "typ": token_type},
        )
        self._record_boundary_secret("mode4_credential", access_token)
        return Mode4Token(
            access_token=access_token,
            installation_id=installation_id,
            expires_at=now + 59,
            variant=variant,
        )

    def mode4_context(self, request: Request):
        headers = {name.lower(): value for name, value in request.headers.items()}
        headers[":method"] = request.method
        headers[":path"] = request.url.path
        try:
            authenticated = self.mode4_adapter.authenticate(RequestContext(headers=headers))
        except IdentityError as exc:
            message = str(exc)
            with self.lock:
                if "tenant" in message:
                    self.mode4_negatives.add("cross_tenant")
                if "installation" in message:
                    self.mode4_negatives.add("cross_installation")
                if "Origin" in message:
                    self.mode4_negatives.add("wrong_origin")
                if "typ must be exactly" in message:
                    self.mode4_negatives.add("wrong_type")
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "Mode 4 identity rejected",
                headers={"Cache-Control": "no-store"},
            ) from exc
        token = headers.get("authorization", "").partition(" ")[2]
        kid = str(jwt.get_unverified_header(token).get("kid") or "")
        with self.lock:
            self.seen_mode4_kids.add(kid)
        return authenticated

    def mode5_context(self, request: Request):
        headers = {name.lower(): value for name, value in request.headers.items()}
        headers[":method"] = request.method
        headers[":path"] = request.url.path
        try:
            result = self.mode5_adapter.authenticate(RequestContext(headers=headers))
        except IdentityError as exc:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "Mode 5 identity rejected",
                headers={"Cache-Control": "no-store"},
            ) from exc
        with self.lock:
            self.mode5_protected = True
        return result

    def create_bff_session(self, persona: Literal["analyst", "auditor"]) -> tuple[str, str]:
        session_cookie = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        session_digest = _digest(session_cookie)
        source_subject = {
            "analyst": "synthetic-analyst-001",
            "auditor": "synthetic-auditor-002",
        }[persona]
        with self.lock:
            self.bff_sessions[session_digest] = _BffSession(
                session_digest=session_digest,
                csrf_digest=_digest(csrf_token),
                source_subject=source_subject,
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
        return session_cookie, csrf_token

    def create_bff_intent(
        self,
        *,
        session: _BffSession,
        instance_id: str,
    ) -> str:
        intent_id = f"intent:{secrets.token_urlsafe(24)}"
        with self.lock:
            self.bff_intents[intent_id] = _BffIntent(
                intent_id=intent_id,
                session_digest=session.session_digest,
                source_subject=session.source_subject,
                instance_id=instance_id,
                expires_at=datetime.now(UTC) + timedelta(minutes=2),
            )
        return intent_id

    def require_bff_session(
        self,
        request: Request,
        *,
        require_csrf: bool,
    ) -> _BffSession:
        origin = request.headers.get("origin", "")
        fetch_site = request.headers.get("sec-fetch-site", "")
        if origin != self.host_origin:
            if origin:
                self.bff_negatives.add("sibling_origin")
            raise _bff_http_error("BFF Origin rejected")
        if fetch_site != "same-origin":
            raise _bff_http_error("BFF fetch metadata rejected")
        raw_cookie = request.cookies.get(_BFF_SESSION_COOKIE, "")
        session = self.bff_sessions.get(_digest(raw_cookie)) if raw_cookie else None
        if session is None or session.expires_at <= datetime.now(UTC):
            self.bff_negatives.add("subject_session_mismatch")
            raise _bff_http_error("BFF session rejected")
        if require_csrf:
            csrf = request.headers.get("x-csrf-token", "")
            if not csrf:
                self.bff_negatives.add("missing_csrf")
                raise _bff_http_error("BFF anti-forgery token required")
            if not hmac.compare_digest(_digest(csrf), session.csrf_digest):
                self.bff_negatives.add("wrong_csrf")
                raise _bff_http_error("BFF anti-forgery token rejected")
        return session

    def authorize_mode5(
        self,
        *,
        session: _BffSession,
        instance_id: str,
        user_intent_id: str,
    ) -> dict[str, object]:
        with self.lock:
            intent = self.bff_intents.get(user_intent_id)
            if (
                intent is None
                or intent.expires_at <= datetime.now(UTC)
                or intent.session_digest != session.session_digest
                or intent.source_subject != session.source_subject
            ):
                self.bff_negatives.add("subject_session_mismatch")
                raise _bff_http_error("BFF session intent rejected")
            if intent.instance_id != instance_id:
                self.bff_negatives.add("instance_mismatch")
                raise _bff_http_error("BFF instance intent rejected")
            if intent.consumed:
                self.bff_negatives.add("duplicate_authorization")
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "BFF intent already consumed",
                    headers={"Cache-Control": "no-store"},
                )
            intent.consumed = True
        now = datetime.now(UTC).replace(microsecond=0)
        subject_token = self._mint_subject_token(now, source_subject=session.source_subject)
        client_assertion = self._mint_client_assertion(now)
        self._record_boundary_secret("mode5_subject_credential", subject_token)
        self._record_boundary_secret("mode5_bff_assertion", client_assertion)
        payload = {
            "installation_id": MODE5_INSTALLATION,
            "instance_id": instance_id,
            "client_id": MODE5_BFF_CLIENT,
            "client_assertion_type": ("urn:ietf:params:oauth:client-assertion-type:jwt-bearer"),
            "client_assertion": client_assertion,
            "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "subject_token": subject_token,
            "requested_scopes": ["cdd.read", "documents.read"],
            "host_proof": {
                "host_origin": self.host_origin,
                "fetch_site": "same-origin",
                "csrf_verified": True,
                "session_binding": session.session_digest,
                "session_source_subject": session.source_subject,
                "user_intent_id": user_intent_id,
            },
        }
        response = httpx.post(
            f"{self.origin}/v1/embed/grants",
            json=payload,
            timeout=10,
        )
        if response.status_code != status.HTTP_200_OK:
            raise RuntimeError(f"Mode 5 authorization failed with {response.status_code}")
        replay = httpx.post(
            f"{self.origin}/v1/embed/grants",
            json=payload,
            timeout=10,
        )
        if replay.status_code != status.HTTP_401_UNAUTHORIZED:
            raise RuntimeError("Mode 5 private_key_jwt replay did not fail closed")
        self.bff_negatives.add("private_key_jwt_replay")
        with self.lock:
            self.mode5_authorized = True
        body = response.json()
        self.sensitive_values.append(str(body["launch_code"]))
        return {"launch_code": body["launch_code"]}

    def _record_boundary_secret(self, category: str, value: str) -> None:
        if not value:
            raise RuntimeError("identity harness tried to record an empty boundary secret")
        digest = hashlib.sha256(value.encode()).hexdigest()
        with self.lock:
            self.boundary_secret_hashes.setdefault(category, set()).add(digest)
            self.sensitive_values.append(value)

    def boundary_secret_digests(self) -> dict[str, tuple[str, ...]]:
        with self.lock:
            return {
                category: tuple(sorted(digests))
                for category, digests in sorted(self.boundary_secret_hashes.items())
            }

    def sqlite_secret_scan(self) -> dict[str, object]:
        database_bytes = b""
        forbidden_columns = {
            "access_token",
            "bff_private_key",
            "client_assertion",
            "launch_code",
            "pkce_verifier",
            "subject_token",
        }
        unsafe_columns: list[str] = []
        for path in (self.flow_path, self.replay_path):
            for sqlite_file in path.parent.glob(f"{path.name}*"):
                if sqlite_file.is_file():
                    database_bytes += sqlite_file.read_bytes()
            if path.exists():
                with sqlite3.connect(path) as connection:
                    tables = connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                    for (table,) in tables:
                        columns = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
                        unsafe_columns.extend(
                            f"{path.name}:{table}.{column[1]}"
                            for column in columns
                            if column[1] in forbidden_columns
                        )
        leaks = tuple(
            f"sensitive-{index}"
            for index, value in enumerate(self.sensitive_values, start=1)
            if value.encode() in database_bytes
        )
        return {
            "safe": not leaks and not unsafe_columns,
            "checked_databases": ["browser-flow.sqlite3", "bff-replay.sqlite3"],
            "leaks": list(leaks),
            "unsafe_secret_columns": unsafe_columns,
        }

    def mode4_evidence(self) -> dict[str, object]:
        report = self.mode4_report
        server = {
            "rsa_jwks_fetches": self.jwks_fetches["rsa"],
            "ec_jwks_fetches": self.jwks_fetches["ec"],
            "verified_kids": sorted(self.seen_mode4_kids),
            "negative_boundaries": sorted(self.mode4_negatives),
            "short_lifetime_seconds": 60,
        }
        expected_negatives = {
            "cross_tenant",
            "cross_installation",
            "wrong_origin",
            "wrong_type",
        }
        ready = bool(
            report
            and all(report.model_dump().values())
            and self.jwks_fetches["rsa"] >= 2
            and self.jwks_fetches["ec"] >= 1
            and {self.rsa_old.kid, self.rsa_rotated.kid, self.ec_active.kid} <= self.seen_mode4_kids
            and expected_negatives <= self.mode4_negatives
        )
        return {
            "status": "ready" if ready else "not_ready",
            "dimension": "identity.mode4",
            "claim_boundary": "channel and identity portability only",
            "browser": report.model_dump() if report else {},
            "server": server,
        }

    def mode5_evidence(self) -> dict[str, object]:
        report = self.mode5_report
        scan = self.sqlite_secret_scan()
        outbox_state_counts = dict(
            sorted(Counter(event.state.value for event in self.flow_store.pending_outbox()).items())
        )
        expected_states = {
            BrowserFlowState.REGISTERED.value,
            BrowserFlowState.CODE_ISSUED.value,
            BrowserFlowState.CONSUMED.value,
        }
        expected_bff_negatives = {
            "sibling_origin",
            "missing_csrf",
            "wrong_csrf",
            "subject_session_mismatch",
            "instance_mismatch",
            "duplicate_authorization",
            "private_key_jwt_replay",
        }
        state_counts = tuple(outbox_state_counts.values())
        ready = bool(
            report
            and all(report.model_dump().values())
            and self.mode5_authorized
            and self.mode5_protected
            and scan["safe"]
            and set(outbox_state_counts) == expected_states
            and bool(state_counts)
            and min(state_counts) > 0
            and len(set(state_counts)) == 1
            and expected_bff_negatives <= self.bff_negatives
        )
        return {
            "status": "ready" if ready else "not_ready",
            "dimension": "identity.mode5",
            "claim_boundary": "channel and identity portability only",
            "browser": report.model_dump() if report else {},
            "server": {
                "bff_private_key_jwt": "production verifier",
                "subject_token": "production RFC 9068 verifier",
                "embed_token": "production dedicated token issuer/verifier",
                "outbox_state_counts": outbox_state_counts,
                "bff_negative_boundaries": sorted(self.bff_negatives),
                "sqlite_secret_scan": scan,
            },
        }

    def _mode4_settings(self) -> Settings:
        base = Settings.load("config/settings.yaml")
        policies = (
            AccessTokenIssuerSettings(
                policy_id="mode4-rsa",
                issuer=self.rsa_issuer,
                jwks_uri=self.rsa_jwks,
                resource_audience=self.mode4_audience,
                tenant=TENANT,
                algorithms=("RS256",),
                allowed_clients=(MODE4_RSA_CLIENT,),
                required_scopes=MODE4_SCOPES,
                client_installations={MODE4_RSA_CLIENT: MODE4_RSA_INSTALLATION},
                max_lifetime_seconds=60,
                clock_skew_seconds=5,
            ),
            AccessTokenIssuerSettings(
                policy_id="mode4-ec",
                issuer=self.ec_issuer,
                jwks_uri=self.ec_jwks,
                resource_audience=self.mode4_audience,
                tenant=TENANT,
                algorithms=("ES256",),
                allowed_clients=(MODE4_EC_CLIENT,),
                required_scopes=MODE4_SCOPES,
                client_installations={MODE4_EC_CLIENT: MODE4_EC_INSTALLATION},
                max_lifetime_seconds=60,
                clock_skew_seconds=5,
            ),
        )
        return replace(
            base,
            identity=replace(
                base.identity,
                mode="oauth-access-token",
                access_token_issuers=policies,
            ),
            channel=ChannelSettings(
                mode="sandboxed",
                public_origin=self.agent_origin,
                installation_manifest=str(self.manifest_path),
                manifest_version="identity-harness-v1",
            ),
        )

    def _mode5_installation(self) -> BrokerInstallationPolicy:
        policy = AccessTokenIssuerSettings(
            policy_id="mode5-subject-rsa",
            issuer=self.rsa_issuer,
            jwks_uri=self.rsa_jwks,
            resource_audience=self.broker_audience,
            tenant=TENANT,
            algorithms=("RS256",),
            allowed_clients=(MODE5_SUBJECT_CLIENT,),
            required_scopes=("embed.grant",),
            client_installations={MODE5_SUBJECT_CLIENT: MODE5_INSTALLATION},
            max_lifetime_seconds=60,
            clock_skew_seconds=5,
        )
        return BrokerInstallationPolicy(
            installation_id=MODE5_INSTALLATION,
            tenant=TENANT,
            protocol_versions=("1",),
            parent_origins=(self.host_origin,),
            permitted_scopes=("cdd.read", "documents.read"),
            subject_grant_scope="embed.grant",
            subject_token_audience=self.broker_audience,
            subject_token_policy=policy,
            bff_clients=(
                BffGrantClientPolicy(
                    client_id=MODE5_BFF_CLIENT,
                    permitted_scopes=("cdd.read", "documents.read"),
                    allowed_subject_clients=(MODE5_SUBJECT_CLIENT,),
                ),
            ),
            registration_lifetime_seconds=120,
        )

    def _write_mode4_manifest(self) -> None:
        def installation(policy: str, client: str) -> dict[str, object]:
            return {
                "tenant": TENANT,
                "parent_origins": [self.host_origin],
                "resource_audience": self.mode4_audience,
                "scopes": list(MODE4_SCOPES),
                "identity_mode": "oauth-access-token",
                "issuer_policy_id": policy,
                "allowed_clients": [client],
                "protocol_versions": ["1"],
                "public_origin": self.agent_origin,
                "public_mount_path": "/agent",
                "loader_version": "v1",
                "fallback_url": f"{self.agent_origin}/agent/",
            }

        document = {
            "schema_version": 1,
            "deployment_manifest_id": "identity-harness-mode4",
            "build_id": "identity-harness-v1",
            "installations": {
                MODE4_RSA_INSTALLATION: installation("mode4-rsa", MODE4_RSA_CLIENT),
                MODE4_EC_INSTALLATION: installation("mode4-ec", MODE4_EC_CLIENT),
            },
        }
        self.manifest_path.write_text(json.dumps(document), encoding="utf-8")

    def _mint_subject_token(self, now: datetime, *, source_subject: str) -> str:
        key = self.active_rsa
        return jwt.encode(
            {
                "iss": self.rsa_issuer,
                "sub": source_subject,
                "aud": self.broker_audience,
                "iat": int((now - timedelta(seconds=1)).timestamp()),
                "exp": int((now + timedelta(seconds=59)).timestamp()),
                "jti": secrets.token_urlsafe(24),
                "client_id": MODE5_SUBJECT_CLIENT,
                "tenant": TENANT,
                "scope": " ".join(MODE5_SCOPES),
                "installation_id": MODE5_INSTALLATION,
            },
            key.private_key,
            algorithm=key.algorithm,
            headers={"kid": key.kid, "typ": "at+jwt"},
        )

    def _mint_client_assertion(self, now: datetime) -> str:
        return jwt.encode(
            {
                "iss": MODE5_BFF_CLIENT,
                "sub": MODE5_BFF_CLIENT,
                "aud": self.broker_audience,
                "iat": int(now.timestamp()),
                "exp": int((now + timedelta(seconds=59)).timestamp()),
                "jti": secrets.token_urlsafe(24),
            },
            self.bff_key.private_key,
            algorithm=self.bff_key.algorithm,
            headers={"kid": self.bff_key.kid, "typ": "JWT"},
        )


def _rsa_key(kid: str) -> _SigningKey:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = (
        private.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    public_jwk.update({"kid": kid, "use": "sig", "alg": "RS256"})
    return _SigningKey(kid, "RS256", private, public_jwk, private_pem, public_pem)


def _ec_key(kid: str) -> _SigningKey:
    private = ec.generate_private_key(ec.SECP256R1())
    private_pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = (
        private.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    public_jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(private.public_key()))
    public_jwk.update({"kid": kid, "use": "sig", "alg": "ES256"})
    return _SigningKey(kid, "ES256", private, public_jwk, private_pem, public_pem)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _bff_http_error(detail: str) -> HTTPException:
    return HTTPException(
        status.HTTP_403_FORBIDDEN,
        detail,
        headers={"Cache-Control": "no-store"},
    )


def _request_context(request: Request) -> RequestContext:
    headers = {name.lower(): value for name, value in request.headers.items()}
    headers[":method"] = request.method
    headers[":path"] = request.url.path
    return RequestContext(headers=headers)


def _app(state: _HarnessState) -> FastAPI:
    app = FastAPI(title="cdd-sow-research synthetic identity evidence harness")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            state.agent_origin,
            state.mode5_agent_origin,
            state.host_origin,
        ],
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "X-CSRF-Token",
            "X-CDD-Installation-ID",
            "X-Request-ID",
        ],
    )

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ready"}

    @app.get("/v1/harness/jwks/{family}")
    def jwks(family: Literal["rsa", "ec"]) -> dict[str, object]:
        return state.jwks(family)

    @app.get("/v1/harness/mode4/tokens/{variant}")
    def mode4_token(
        variant: Literal[
            "valid",
            "refresh",
            "ec",
            "cross-tenant",
            "cross-installation",
            "wrong-type",
        ],
        response: Response,
    ) -> dict[str, object]:
        response.headers["Cache-Control"] = "no-store"
        token = state.mint_mode4(variant)
        return {
            "access_token": token.access_token,
            "token_type": "Bearer",
            "installation_id": token.installation_id,
            "expires_at": token.expires_at,
            "variant": token.variant,
        }

    @app.post("/v1/harness/mode4/rotate")
    def rotate_mode4(response: Response) -> dict[str, str]:
        response.headers["Cache-Control"] = "no-store"
        return {"active_kid": state.rotate_rsa()}

    def mode4_identity(request: Request):
        return state.mode4_context(request)

    @app.post("/v1/harness/mode4/protected/json")
    async def mode4_json(
        request: Request,
        identity: Any = Depends(mode4_identity),  # noqa: B008
    ) -> dict[str, object]:
        payload = await request.json()
        return {
            "status": "accepted",
            "transport": "json",
            "tenant": identity.principal.tenant,
            "installation_id": identity.evidence.installation,
            "payload_type": payload.get("type", ""),
        }

    @app.post("/v1/harness/mode4/protected/form")
    def mode4_form(
        note: str = Form(),  # noqa: B008
        identity: Any = Depends(mode4_identity),  # noqa: B008
    ) -> dict[str, object]:
        return {
            "status": "accepted",
            "transport": "form-data",
            "tenant": identity.principal.tenant,
            "installation_id": identity.evidence.installation,
            "note_length": len(note),
        }

    @app.post("/v1/harness/mode4/protected/blob")
    async def mode4_blob(
        request: Request,
        identity: Any = Depends(mode4_identity),  # noqa: B008
    ) -> Response:
        body = await request.body()
        return Response(
            b"%PDF-1.4\n%cdd-sow-research identity harness\n" + base64.b64encode(body),
            media_type="application/pdf",
            headers={
                "Cache-Control": "no-store",
                "X-CDD-Installation-ID": identity.evidence.installation,
            },
        )

    app.include_router(create_embed_router(state.broker_dependencies))

    @app.post("/v1/harness/bff/session")
    def bff_session(
        request: Request,
        response: Response,
        persona: Literal["analyst", "auditor"] = "analyst",
    ) -> dict[str, object]:
        if (
            request.headers.get("origin", "") != state.host_origin
            or request.headers.get("sec-fetch-site", "") != "same-origin"
        ):
            if request.headers.get("origin"):
                state.bff_negatives.add("sibling_origin")
            raise _bff_http_error("BFF session origin rejected")
        session_cookie, csrf_token = state.create_bff_session(persona)
        response.set_cookie(
            _BFF_SESSION_COOKIE,
            session_cookie,
            max_age=300,
            httponly=True,
            secure=False,
            samesite="lax",
            path="/",
        )
        response.headers["Cache-Control"] = "no-store"
        return {
            "status": "authenticated",
            "csrf_token": csrf_token,
            "expires_in": 300,
        }

    @app.post("/v1/harness/bff/intents")
    def bff_intent(
        body: _Mode5IntentRequest,
        request: Request,
    ) -> dict[str, str]:
        session = state.require_bff_session(request, require_csrf=True)
        intent_id = state.create_bff_intent(
            session=session,
            instance_id=body.instance_id,
        )
        return {"status": "intent-recorded", "user_intent_id": intent_id}

    @app.post("/v1/harness/bff/authorize")
    def mode5_authorize(
        body: _Mode5AuthorizeRequest,
        request: Request,
    ) -> dict[str, object]:
        session = state.require_bff_session(request, require_csrf=True)
        return state.authorize_mode5(
            session=session,
            instance_id=body.instance_id,
            user_intent_id=body.user_intent_id,
        )

    def mode5_identity(request: Request):
        return state.mode5_context(request)

    @app.post("/v1/harness/mode5/protected")
    async def mode5_protected(
        request: Request,
        identity: Any = Depends(mode5_identity),  # noqa: B008
    ) -> dict[str, object]:
        payload = await request.json()
        return {
            "status": "accepted",
            "transport": "json",
            "tenant": identity.principal.tenant,
            "installation_id": identity.verified_token.installation_id,
            "payload_type": payload.get("type", ""),
        }

    @app.get("/v1/cases/{case_id}/documents")
    def mode5_document_list(
        case_id: str,
        identity: Any = Depends(mode5_identity),  # noqa: B008
    ) -> dict[str, object]:
        del case_id, identity
        return {"documents": []}

    @app.post("/v1/harness/evidence/mode4")
    def report_mode4(report: _Mode4ReportRequest) -> dict[str, str]:
        state.mode4_report = Mode4BrowserEvidence.model_validate(report.model_dump())
        return {"status": "recorded"}

    @app.post("/v1/harness/evidence/mode5")
    def report_mode5(report: _Mode5ReportRequest) -> dict[str, str]:
        state.mode5_report = Mode5BrowserEvidence.model_validate(report.model_dump())
        return {"status": "recorded"}

    @app.get("/v1/harness/evidence/mode4")
    def mode4_evidence() -> JSONResponse:
        return JSONResponse(
            state.mode4_evidence(),
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/v1/harness/evidence/mode5")
    def mode5_evidence() -> JSONResponse:
        return JSONResponse(
            state.mode5_evidence(),
            headers={"Cache-Control": "no-store"},
        )

    return app


class _UvicornService:
    def __init__(self, app: FastAPI, *, port: int) -> None:
        self.server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=port,
                log_level="warning",
                access_log=False,
            )
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self) -> None:
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            if not self.thread.is_alive():
                raise RuntimeError("identity harness service exited during startup")
            if time.monotonic() >= deadline:
                raise RuntimeError("identity harness service did not become ready")
            time.sleep(0.02)

    def close(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)
        if self.thread.is_alive():
            raise RuntimeError("identity harness service did not stop")


class IdentityHarness:
    """Lifecycle and BFF-side API used by the browser evidence runner.

    Construct this before the agent-edge proxy so ``api_origin`` can be configured as
    its upstream. The edge must preserve ``Origin``, ``Authorization`` and
    ``X-CDD-Installation-ID``.
    """

    def __init__(
        self,
        *,
        agent_origin: str = "http://127.0.0.1:3200",
        mode5_agent_origin: str | None = None,
        host_origin: str = "http://127.0.0.1:4101",
    ) -> None:
        self.agent_origin = agent_origin.rstrip("/")
        self.mode5_agent_origin = (mode5_agent_origin or agent_origin).rstrip("/")
        self.host_origin = host_origin.rstrip("/")
        self._temporary = tempfile.TemporaryDirectory(prefix="doc1-identity-harness-")
        self._port = _free_port()
        self.api_origin = f"http://127.0.0.1:{self._port}"
        self._state = _HarnessState(
            Path(self._temporary.name),
            origin=self.api_origin,
            agent_origin=self.agent_origin,
            mode5_agent_origin=self.mode5_agent_origin,
            host_origin=self.host_origin,
        )
        self._service = _UvicornService(_app(self._state), port=self._port)
        self._started = False

    @property
    def mode4_evidence_url(self) -> str:
        return f"{self.api_origin}/v1/harness/evidence/mode4"

    @property
    def mode5_evidence_url(self) -> str:
        return f"{self.api_origin}/v1/harness/evidence/mode5"

    @property
    def hook_environment(self) -> dict[str, str]:
        return {
            "CDD_MODE4_EVIDENCE_URL": self.mode4_evidence_url,
            "CDD_MODE5_EVIDENCE_URL": self.mode5_evidence_url,
        }

    @property
    def api_targets(self) -> dict[str, str]:
        """Canonical installation-to-upstream map for the agent-edge proxy."""
        return {
            MODE4_RSA_INSTALLATION: self.api_origin,
            MODE4_EC_INSTALLATION: self.api_origin,
            MODE5_INSTALLATION: self.api_origin,
        }

    @property
    def bff_paths(self) -> dict[str, str]:
        """Paths the host-A same-origin reverse proxy exposes under ``/bff``."""
        return {
            "session": "/v1/harness/bff/session",
            "intent": "/v1/harness/bff/intents",
            "authorize": "/v1/harness/bff/authorize",
        }

    def start(self) -> IdentityHarness:
        if not self._started:
            jwks_verify._cache.clear()
            self._service.start()
            self._started = True
        return self

    def close(self) -> None:
        if self._started:
            self._service.close()
            self._started = False
        self._temporary.cleanup()

    def __enter__(self) -> IdentityHarness:
        return self.start()

    def __exit__(self, *_error: object) -> None:
        self.close()

    def mint_mode4_token(self, variant: str = "valid") -> Mode4Token:
        return self._state.mint_mode4(variant)

    def rotate_mode4_issuer(self) -> str:
        return self._state.rotate_rsa()

    def register_mode5(self, pkce_challenge: str) -> dict[str, object]:
        response = httpx.post(
            f"{self.api_origin}/v1/embed/instances",
            json={
                "installation_id": MODE5_INSTALLATION,
                "protocol_version": "1",
                "pkce_challenge": pkce_challenge,
                "pkce_method": "S256",
            },
            timeout=10,
        )
        response.raise_for_status()
        return response.json()

    def redeem_mode5(
        self,
        instance_id: str,
        launch_code: str,
        pkce_verifier: str,
    ) -> httpx.Response:
        self._state.sensitive_values.append(launch_code)
        response = httpx.post(
            f"{self.api_origin}/v1/embed/token",
            json={
                "installation_id": MODE5_INSTALLATION,
                "instance_id": instance_id,
                "launch_code": launch_code,
                "pkce_verifier": pkce_verifier,
            },
            timeout=10,
        )
        return response

    def record_mode4_browser_evidence(self, **evidence: bool) -> None:
        self._state.mode4_report = Mode4BrowserEvidence.model_validate(evidence)

    def record_mode5_browser_evidence(self, **evidence: bool) -> None:
        self._state.mode5_report = Mode5BrowserEvidence.model_validate(evidence)

    def mode4_evidence(self) -> dict[str, object]:
        return self._state.mode4_evidence()

    def mode5_evidence(self) -> dict[str, object]:
        return self._state.mode5_evidence()

    def sqlite_secret_scan(self) -> dict[str, object]:
        return self._state.sqlite_secret_scan()

    def boundary_secret_digests(self) -> dict[str, tuple[str, ...]]:
        """Return only hashes of credentials seen by the production identity chain."""
        return self._state.boundary_secret_digests()


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@contextmanager
def running_identity_harness(
    *,
    agent_origin: str = "http://127.0.0.1:3200",
    mode5_agent_origin: str | None = None,
    host_origin: str = "http://127.0.0.1:4101",
) -> Iterator[IdentityHarness]:
    """Convenience function for runner composition."""
    with IdentityHarness(
        agent_origin=agent_origin,
        mode5_agent_origin=mode5_agent_origin,
        host_origin=host_origin,
    ) as harness:
        yield harness
