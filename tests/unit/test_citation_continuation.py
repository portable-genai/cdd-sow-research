from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from starlette.requests import Request

from cdd_sow_research.adapters.local.browser_flow_store import LocalSQLiteBrowserFlowStore
from cdd_sow_research.adapters.local.case_store import LocalCaseStoreAdapter
from cdd_sow_research.adapters.local.document_store import LocalDocumentStoreAdapter
from cdd_sow_research.adapters.oidc import session_token
from cdd_sow_research.api import auth, deps
from cdd_sow_research.api import citation_continuation as continuation
from cdd_sow_research.api.app import assess_cdd
from cdd_sow_research.api.citation_ids import (
    citation_identifier_from_url,
    decode_citation_reference,
)
from cdd_sow_research.api.deps import build_cdd_service
from cdd_sow_research.api.schemas import CddCaseResponse, CddRequest, DocumentModel, SubjectModel
from cdd_sow_research.api.security import (
    AuthenticatedContext,
    IdentityEvidence,
    canonical_actor,
)
from cdd_sow_research.config import (
    AccessTokenIssuerSettings,
    ChannelSettings,
    Container,
    IdentitySettings,
    LocalSettings,
    Settings,
)
from cdd_sow_research.domain.browser_flow import (
    BrowserFlowState,
    BrowserFlowStateError,
    CitationLedgerEntry,
)
from cdd_sow_research.domain.errors import CaseAccessDeniedError
from cdd_sow_research.domain.identity import Principal
from cdd_sow_research.domain.models import (
    Citation,
    DocType,
    EvidenceItem,
    SourceType,
    SowCase,
    Subject,
)

SOURCE_ACTOR = canonical_actor("https://embed-idp.example", "subject-123")
MODE6_ACTOR = canonical_actor("https://standalone-idp.example", "subject-789")


@dataclass
class _Container:
    settings: Settings
    case_store: LocalCaseStoreAdapter
    document_store: LocalDocumentStoreAdapter
    browser_flow_store: LocalSQLiteBrowserFlowStore


def _manifest(
    path: Path,
    *,
    fallback_url: str = "https://standalone.example/agent/",
) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "deployment_manifest_id": "citation-proof",
                "build_id": "build-citation-proof",
                "installations": {
                    "inst_demo_bank": {
                        "tenant": "demo-bank",
                        "parent_origins": ["https://portal.demo-bank.example"],
                        "resource_audience": "https://embed.example/api",
                        "scopes": ["cdd.read", "documents.read"],
                        "identity_mode": "oauth-access-token",
                        "issuer_policy_id": "mode4-policy",
                        "allowed_clients": ["portal-bff"],
                        "protocol_versions": ["1"],
                        "public_origin": "https://embed.example",
                        "public_mount_path": "/agent",
                        "loader_version": "v1",
                        "fallback_url": fallback_url,
                    }
                },
            }
        )
    )
    return path


def _settings(
    tmp_path: Path,
    *,
    fallback_url: str = "https://standalone.example/agent/",
) -> Settings:
    base = Settings.load("config/settings.yaml")
    policy = AccessTokenIssuerSettings(
        policy_id="mode4-policy",
        issuer="https://embed-idp.example",
        jwks_uri="https://embed-idp.example/jwks",
        resource_audience="https://embed.example/api",
        tenant="demo-bank",
        allowed_clients=("portal-bff",),
        required_scopes=("cdd.read", "documents.read"),
    )
    return Settings(
        **{
            **base.__dict__,
            "local": LocalSettings(
                db_path=":memory:",
                documents_path=str(tmp_path / "documents.sqlite3"),
                browser_flow_path=str(tmp_path / "browser-flows.sqlite3"),
            ),
            "identity": IdentitySettings(
                mode="oauth-access-token",
                access_token_issuers=(policy,),
                citation_subject_links={SOURCE_ACTOR: MODE6_ACTOR},
                bindings=base.identity.bindings,
            ),
            "channel": ChannelSettings(
                mode="sandboxed",
                public_origin="https://embed.example",
                installation_manifest=str(
                    _manifest(
                        tmp_path / "installations.json",
                        fallback_url=fallback_url,
                    )
                ),
                manifest_version="v1",
            ),
        }
    )


def _principal(actor: str = SOURCE_ACTOR, tenant: str = "demo-bank") -> Principal:
    return Principal(
        subject=actor,
        principals=("group:cdd-analyst", f"user:{actor}"),
        tenant=tenant,
        assurance="mfa",
        source="oauth-access-token",
    )


def _context() -> AuthenticatedContext:
    return AuthenticatedContext(
        principal=_principal(),
        evidence=IdentityEvidence(
            issuer="https://embed-idp.example",
            source_subject="subject-123",
            token_type="at+jwt",
            authorized_client="portal-bff",
            effective_scopes=("cdd.read", "documents.read"),
            installation="inst_demo_bank",
            correlation="corr-citation-proof",
        ),
    )


def _install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    fallback_url: str = "https://standalone.example/agent/",
):
    settings = _settings(tmp_path, fallback_url=fallback_url)
    case_store = LocalCaseStoreAdapter(settings)
    document_store = LocalDocumentStoreAdapter(settings)
    browser_store = LocalSQLiteBrowserFlowStore(settings)
    document = document_store.put(
        b"evidence bytes",
        "evidence.pdf",
        DocType.BANK_STATEMENT,
        "case-123",
        ("case:case-123", "tenant:demo-bank"),
        "application/pdf",
    )
    citation = Citation(
        source_id=document.id,
        source_type=SourceType.DOCUMENT,
        title="Evidence",
        url=document.uri,
        page=1,
    )
    case = SowCase(
        id="case-123",
        subject=Subject(id="case-123", name="Case", tenant="demo-bank"),
        ledger=(
            EvidenceItem(
                id=document.id,
                document=document.to_kyc_document(),
                citations=(citation,),
            ),
        ),
    )
    case_store.open(case)
    container = _Container(settings, case_store, document_store, browser_store)
    monkeypatch.setattr(deps, "get_container", lambda: container)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    citation_id = citation_identifier_from_url(
        citation.url,
        source_id=citation.source_id,
        page=citation.page,
    )
    assert citation_id
    reference = decode_citation_reference(citation_id)
    browser_store.record_citations(
        (
            CitationLedgerEntry(
                citation_id=citation_id,
                tenant="demo-bank",
                source_actor=SOURCE_ACTOR,
                case_id=reference.case_id,
                evidence_id=reference.evidence_id,
                source_id=reference.source_id,
                page=reference.page,
            ),
        )
    )
    return container, citation_id


def _register(citation_id: str):
    response = continuation.register_citation_continuation(citation_id, _context())
    body = json.loads(response.body)
    return body["continuation_url"]


def _mode6_context() -> AuthenticatedContext:
    return AuthenticatedContext(
        principal=_principal(MODE6_ACTOR),
        evidence=IdentityEvidence(
            issuer="https://standalone-idp.example",
            source_subject="subject-789",
            token_type="session",
            session_jti="mode6-session-jti",
        ),
    )


def _post_request(path: str, *, cookie: str = "") -> Request:
    headers = [(b"cookie", cookie.encode())] if cookie else []
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 1234),
            "server": ("standalone.example", 443),
        }
    )


def test_registration_returns_only_manifest_owned_fragment_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _container, citation_id = _install(tmp_path, monkeypatch)

    url = _register(citation_id)

    assert url.startswith("https://standalone.example/agent/auth/citation#")
    assert "case-123" not in url
    assert "/documents/" not in url
    assert (
        "no-store"
        in continuation.register_citation_continuation(citation_id, _context()).headers[
            "cache-control"
        ]
    )


@pytest.mark.parametrize(
    ("fallback_url", "expected"),
    [
        (
            "https://standalone.example/agent/login",
            "https://standalone.example/agent/auth/citation#ticket",
        ),
        (
            "http://127.0.0.1:3300/agent/",
            "http://127.0.0.1:3300/agent/auth/citation#ticket",
        ),
    ],
)
def test_continuation_uses_fixed_agent_route_on_allowed_origin(
    fallback_url: str,
    expected: str,
) -> None:
    assert continuation._continuation_url(fallback_url, "ticket") == expected


def test_loopback_development_fallback_completes_registration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _container, citation_id = _install(
        tmp_path,
        monkeypatch,
        fallback_url="http://127.0.0.1:3300/agent/login",
    )

    url = _register(citation_id)

    assert url.startswith("http://127.0.0.1:3300/agent/auth/citation#")


def test_real_one_shot_assessment_emits_only_resolvable_continuations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    container = Container(settings)
    principal = _principal()
    document = container.document_store.put(
        b"ACME shareholder wealth from a documented property sale.",
        "source.txt",
        DocType.OTHER,
        "case-real-assessment",
        ("case:case-real-assessment", "tenant:demo-bank"),
        "text/plain",
    )
    monkeypatch.setattr(deps, "get_container", lambda: container)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    response = assess_cdd(
        CddRequest(
            subject=SubjectModel(
                id="case-real-assessment",
                name="ACME Real Assessment",
                type="entity",
                jurisdiction="SG",
            ),
            documents=[
                DocumentModel(
                    id=document.id,
                    doc_type=document.doc_type.value,
                    uri=document.uri,
                    acl_tags=list(document.acl_tags),
                )
            ],
        ),
        principal,
        build_cdd_service(container),
    )
    assert isinstance(response, CddCaseResponse)
    emitted = [
        citation.continuation_id for citation in response.sow.citations if citation.continuation_id
    ]

    assert emitted
    registered = continuation.register_citation_continuation(emitted[0], _context())
    assert json.loads(registered.body)["continuation_url"].startswith(
        "https://standalone.example/agent/auth/citation#"
    )


def test_landing_removes_fragment_before_body_post() -> None:
    response = continuation.citation_landing()
    body = response.body.decode()

    assert body.index("history.replaceState") < body.index(".submit();")
    assert 'method="post"' in body
    assert "/agent/auth/citation/start" in body
    assert response.headers["referrer-policy"] == "no-referrer"


def test_restart_then_exact_callback_consumes_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    container, citation_id = _install(tmp_path, monkeypatch)
    continuation_url = _register(citation_id)
    ticket = continuation_url.split("#", 1)[1]
    restarted = LocalSQLiteBrowserFlowStore(container.settings)
    container.browser_flow_store = restarted
    pending = restarted.begin_citation(
        ticket,
        auth_transaction_id="auth-txn-1",
        as_of=datetime.now(UTC),
    )
    txn = {
        "citation_record_id": pending.record_id,
        "citation_installation_id": "inst_demo_bank",
        "citation_auth_transaction_id": "auth-txn-1",
    }

    consumed = continuation.complete_citation_callback(
        container.settings,
        txn,
        _principal(MODE6_ACTOR),
    )

    assert consumed is not None and consumed.state is BrowserFlowState.CONSUMED
    with pytest.raises(Exception) as replay:
        continuation.complete_citation_callback(
            container.settings,
            txn,
            _principal(MODE6_ACTOR),
        )
    assert isinstance(replay.value.__cause__, BrowserFlowStateError)


def test_citation_start_route_consumes_one_time_ticket(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _container, citation_id = _install(tmp_path, monkeypatch)
    ticket = _register(citation_id).split("#", 1)[1]
    monkeypatch.setattr(auth, "_require_oidc_session", lambda _settings: None)
    monkeypatch.setattr(auth, "_select_issuer", lambda _identity, _tenant: object())
    monkeypatch.setattr(auth, "_client_secret", lambda _issuer: "not-used")
    monkeypatch.setattr(auth, "_validated_discovery", lambda _issuer: object())
    monkeypatch.setattr(
        auth,
        "oidc_authorization_redirect",
        lambda *_args, **_kwargs: continuation.RedirectResponse(
            "https://standalone-idp.example/authorize",
            status_code=302,
        ),
    )

    response = continuation.start_citation_login(ticket)

    assert response.status_code == 302
    with pytest.raises(Exception) as replay:
        continuation.start_citation_login(ticket)
    assert replay.value.status_code == 400


def test_post_callback_continue_requires_txn_and_deletes_it_after_redirect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The only case in this module that mints a real txn cookie: `session_token.mint`
    # lazily imports PyJWT, which ships with the optional [oidc] extra.
    pytest.importorskip("jwt")
    container, citation_id = _install(tmp_path, monkeypatch)
    ticket = _register(citation_id).split("#", 1)[1]
    pending = container.browser_flow_store.begin_citation(
        ticket,
        auth_transaction_id="auth-txn-continue",
        as_of=datetime.now(UTC),
    )
    continuation.complete_citation_callback(
        container.settings,
        {
            "citation_record_id": pending.record_id,
            "citation_installation_id": "inst_demo_bank",
            "citation_auth_transaction_id": "auth-txn-continue",
        },
        _principal(MODE6_ACTOR),
    )
    signing_env = container.settings.identity.session_signing_key_env
    monkeypatch.setenv(signing_env, "citation-session-signing-key-at-least-32-bytes")
    txn = session_token.mint(
        {
            "citation_record_id": pending.record_id,
            "citation_auth_transaction_id": "auth-txn-continue",
        },
        typ="txn",
        signing_key_env=signing_env,
        ttl_seconds=60,
    )

    response = continuation.continue_to_citation(
        _post_request(
            "/auth/citation/continue",
            cookie=f"{session_token.TXN_COOKIE_NAME}={txn}",
        ),
        _mode6_context(),
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("https://embed.example/agent/api/v1/cases/")
    assert session_token.TXN_COOKIE_NAME in response.headers["set-cookie"]
    assert "Max-Age=0" in response.headers["set-cookie"]
    with pytest.raises(Exception) as missing:
        continuation.continue_to_citation(
            _post_request("/auth/citation/continue"),
            _mode6_context(),
        )
    assert missing.value.status_code == 400


@pytest.mark.parametrize(
    "principal",
    [
        _principal(canonical_actor("https://standalone-idp.example", "wrong")),
        _principal(MODE6_ACTOR, tenant="other-bank"),
    ],
)
def test_callback_rejects_wrong_actor_or_tenant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    principal: Principal,
) -> None:
    container, citation_id = _install(tmp_path, monkeypatch)
    ticket = _register(citation_id).split("#", 1)[1]
    pending = container.browser_flow_store.begin_citation(
        ticket,
        auth_transaction_id="auth-txn-1",
        as_of=datetime.now(UTC),
    )

    with pytest.raises(Exception) as rejected:
        continuation.complete_citation_callback(
            container.settings,
            {
                "citation_record_id": pending.record_id,
                "citation_installation_id": "inst_demo_bank",
                "citation_auth_transaction_id": "auth-txn-1",
            },
            principal,
        )

    assert rejected.value.status_code == 403
    assert (
        container.browser_flow_store.get(pending.record_id).state is BrowserFlowState.AUTH_PENDING
    )


def test_changed_evidence_authorization_fails_closed_after_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    container, citation_id = _install(tmp_path, monkeypatch)
    ticket = _register(citation_id).split("#", 1)[1]
    pending = container.browser_flow_store.begin_citation(
        ticket,
        auth_transaction_id="auth-txn-1",
        as_of=datetime.now(UTC),
    )
    document_id = pending.registration.evidence_id
    container.document_store.delete(
        document_id,
        ("group:cdd-analyst", "tenant:demo-bank", "case:case-123"),
    )

    with pytest.raises(Exception) as rejected:
        continuation.complete_citation_callback(
            container.settings,
            {
                "citation_record_id": pending.record_id,
                "citation_installation_id": "inst_demo_bank",
                "citation_auth_transaction_id": "auth-txn-1",
            },
            _principal(MODE6_ACTOR),
        )

    assert rejected.value.status_code == 403


def test_target_policy_rejects_non_https_or_unowned_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    container, citation_id = _install(tmp_path, monkeypatch)

    with pytest.raises(CaseAccessDeniedError, match="HTTPS"):
        continuation._authorized_target(
            container.settings,
            _principal(),
            citation_id,
            source_actor=SOURCE_ACTOR,
            target_origin="http://standalone.example",
        )
    target = continuation._authorized_target(
        container.settings,
        _principal(),
        citation_id,
        source_actor=SOURCE_ACTOR,
        target_origin="https://standalone.example",
    )
    assert target.startswith("https://standalone.example/agent/api/v1/cases/")
    assert target.endswith("#page=1")


def test_registration_requires_reviewed_subject_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    container, citation_id = _install(tmp_path, monkeypatch)
    container.settings = Settings(
        **{
            **container.settings.__dict__,
            "identity": IdentitySettings(
                mode="oauth-access-token",
                access_token_issuers=container.settings.identity.access_token_issuers,
                citation_subject_links={},
                bindings=container.settings.identity.bindings,
            ),
        }
    )

    with pytest.raises(Exception) as rejected:
        continuation.register_citation_continuation(citation_id, _context())

    assert rejected.value.status_code == 403
