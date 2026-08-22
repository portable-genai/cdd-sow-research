"""Opaque Mode 6 continuation for opening an authorized citation original.

The embedded caller submits one server-owned citation identifier. The original URL is
resolved and authorized only inside this module and is never returned to host JavaScript.
The browser receives a short-lived opaque ticket only in a Mode 6 URL fragment.
"""

from __future__ import annotations

import html
import ipaddress
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ..adapters.oidc import session_token
from ..config import PUBLIC_MOUNT_PATH, Settings
from ..domain import entitlements
from ..domain import models as m
from ..domain.browser_flow import (
    BrowserFlowBindingError,
    BrowserFlowError,
    BrowserFlowState,
    CitationContinuationRecord,
    CitationFlowRegistration,
    CitationLedgerEntry,
)
from ..domain.errors import CaseAccessDeniedError, DocumentNotFoundError
from ..domain.identity import Principal
from . import deps
from .citation_ids import (
    citation_identifier_from_url,
    decode_citation_reference,
)
from .security import CurrentAuthenticatedContext, require_scopes

router = APIRouter(tags=["citation-continuation"])

_LANDING_PATH = f"{PUBLIC_MOUNT_PATH}/auth/citation"
_START_PATH = f"{_LANDING_PATH}/start"
_CONTINUE_PATH = f"{_LANDING_PATH}/continue"
_TICKET_LIFETIME = timedelta(seconds=60)


def _private_no_store_headers() -> dict[str, str]:
    return {
        "Cache-Control": "private, no-store",
        "Pragma": "no-cache",
        "Referrer-Policy": "no-referrer",
    }


def _http_error(code: int, message: str) -> HTTPException:
    return HTTPException(code, message)


def _mode6_origin(fallback_url: str) -> str:
    parsed = urlsplit(fallback_url)
    if (
        not parsed.hostname
        or parsed.username
        or parsed.password
        or not _allowed_scheme(parsed.scheme, parsed.hostname)
    ):
        raise ValueError("Mode 6 fallback must use HTTPS except on loopback")
    default_port = (parsed.scheme == "https" and parsed.port == 443) or (
        parsed.scheme == "http" and parsed.port == 80
    )
    port = f":{parsed.port}" if parsed.port is not None and not default_port else ""
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    return f"{parsed.scheme}://{host}{port}"


def _allowed_scheme(scheme: str, hostname: str) -> bool:
    if scheme == "https":
        return True
    if scheme != "http":
        return False
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _continuation_url(fallback_url: str, opaque_ticket: str) -> str:
    # The manifest owns the Mode 6 origin. The path is fixed so an optional fallback
    # leaf can never redirect citation continuation onto a different application route.
    base = f"{_mode6_origin(fallback_url)}{_LANDING_PATH}"
    return f"{base}#{opaque_ticket}"


def _expected_actor(settings: Settings, source_actor: str) -> str:
    expected = settings.identity.citation_subject_links.get(source_actor, "")
    if not expected:
        raise CaseAccessDeniedError(
            "embedded actor has no reviewed issuer-qualified Mode 6 subject link"
        )
    # Canonical actors are deliberately opaque. Reject email-like or free-form links.
    from .security import decode_canonical_actor

    if decode_canonical_actor(source_actor) is None or decode_canonical_actor(expected) is None:
        raise CaseAccessDeniedError("citation subject link is not issuer-qualified")
    return expected


def record_cdd_citations(case: m.CDDCase, principal: Principal) -> frozenset[str]:
    """Persist the exact document citations emitted by one real CDD assessment."""
    citations: list[m.Citation] = list(case.sow.citations) + list(case.rating.citations)
    for source in case.sow.sources:
        citations.extend(source.citations)
    for factor in case.rating.factors:
        citations.extend(factor.citations)
    for finding in case.adverse_media.findings if case.adverse_media is not None else ():
        if finding.citation is not None:
            citations.append(finding.citation)
    if case.ownership is not None:
        citations.extend(case.ownership.citations)
        for owner in case.ownership.owners:
            citations.extend(owner.citations)

    entries: dict[str, CitationLedgerEntry] = {}
    for citation in citations:
        citation_id = citation_identifier_from_url(
            citation.url,
            source_id=citation.source_id,
            page=citation.page,
        )
        if not citation_id:
            continue
        reference = decode_citation_reference(citation_id)
        if reference.case_id != case.subject.id:
            raise ValueError("CDD citation case binding does not match the assessment")
        scope = entitlements.case_scope(principal, reference.case_id)
        try:
            document = deps.get_container().document_store.metadata(
                reference.source_id,
                scope,
            )
        except DocumentNotFoundError:
            continue
        if document.subject_id != reference.case_id:
            continue
        entry = CitationLedgerEntry(
            citation_id=citation_id,
            tenant=principal.tenant,
            source_actor=principal.actor,
            case_id=reference.case_id,
            evidence_id=reference.evidence_id,
            source_id=reference.source_id,
            page=reference.page,
        )
        existing = entries.get(citation_id)
        if existing is not None and existing != entry:
            raise ValueError("CDD citation identifier has conflicting evidence bindings")
        entries[citation_id] = entry
    ordered = tuple(entries[key] for key in sorted(entries))
    deps.get_container().browser_flow_store.record_citations(ordered)
    return frozenset(entries)


def _authorized_target(
    settings: Settings,
    principal: Principal,
    citation_id: str,
    *,
    source_actor: str,
    target_origin: str,
) -> str:
    """Reauthorize emitted citation, current document, and server-owned HTTPS target."""
    try:
        reference = decode_citation_reference(citation_id)
    except ValueError as exc:
        raise CaseAccessDeniedError("citation identifier is not recognized") from exc
    scope = entitlements.case_scope(principal, reference.case_id)
    container = deps.get_container()
    emitted = container.browser_flow_store.get_citation(
        citation_id,
        tenant=principal.tenant,
        source_actor=source_actor,
    )
    if (
        emitted.case_id != reference.case_id
        or emitted.evidence_id != reference.evidence_id
        or emitted.source_id != reference.source_id
        or emitted.page != reference.page
    ):
        raise CaseAccessDeniedError("emitted citation binding changed")
    try:
        document = container.document_store.metadata(reference.source_id, scope)
    except DocumentNotFoundError as exc:
        raise CaseAccessDeniedError("citation evidence is not authorized") from exc
    if document.subject_id != reference.case_id:
        raise CaseAccessDeniedError("citation evidence case binding changed")

    parsed_origin = urlsplit(target_origin)
    if (
        not parsed_origin.hostname
        or not _allowed_scheme(parsed_origin.scheme, parsed_origin.hostname)
        or not parsed_origin.netloc
        or parsed_origin.path not in ("", "/")
        or parsed_origin.query
        or parsed_origin.fragment
    ):
        raise CaseAccessDeniedError("citation target origin violates HTTPS source policy")
    target = (
        target_origin.rstrip("/")
        + PUBLIC_MOUNT_PATH
        + "/api"
        + document.uri
        + (f"#page={reference.page}" if reference.page is not None else "")
    )
    parsed_target = urlsplit(target)
    if (
        parsed_target.scheme != parsed_origin.scheme
        or parsed_target.netloc != parsed_origin.netloc
        or not parsed_target.path.startswith(f"{PUBLIC_MOUNT_PATH}/api/v1/cases/")
        or parsed_target.query
    ):
        raise CaseAccessDeniedError("citation target violates the server-owned source policy")
    return target


@router.post(
    "/v1/embed/citations/{citation_id}/continuations",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_scopes("cdd.read", "documents.read"))],
)
def register_citation_continuation(
    citation_id: str,
    context: CurrentAuthenticatedContext,
) -> JSONResponse:
    """Create one actor-bound ticket; return no original URL or caller redirect."""
    container = deps.get_container()
    settings = container.settings
    if settings.channel_mode != "sandboxed" or settings.identity_mode not in {
        "oauth-access-token",
        "embedded-grant",
    }:
        raise _http_error(status.HTTP_404_NOT_FOUND, "citation continuation is disabled")
    installation_id = context.evidence.installation
    if not installation_id:
        raise _http_error(
            status.HTTP_401_UNAUTHORIZED,
            "verified identity is not bound to an installation",
        )
    try:
        loaded = settings.installation_manifest()
        installation = loaded.manifest.resolve(installation_id)
        if context.principal.tenant != installation.tenant:
            raise CaseAccessDeniedError("installation tenant does not match the caller")
        if not set(installation.scopes).issubset(context.evidence.effective_scopes):
            raise CaseAccessDeniedError("caller lacks the installation citation scopes")
        target_origin = _mode6_origin(installation.fallback_url)
        _authorized_target(
            settings,
            context.principal,
            citation_id,
            source_actor=context.principal.actor,
            target_origin=target_origin,
        )
        expected_actor = _expected_actor(settings, context.principal.actor)
        now = datetime.now(UTC)
        registered = container.browser_flow_store.register_citation(
            CitationFlowRegistration(
                installation_id=installation.installation_id,
                tenant=installation.tenant,
                source_actor=context.principal.actor,
                expected_actor=expected_actor,
                case_id=decode_citation_reference(citation_id).case_id,
                evidence_id=decode_citation_reference(citation_id).evidence_id,
                citation_id=citation_id,
                correlation_id=context.evidence.correlation or secrets.token_urlsafe(16),
            ),
            now=now,
            expires_at=now + _TICKET_LIFETIME,
        )
        location = _continuation_url(installation.fallback_url, registered.opaque_token)
    except (BrowserFlowError, CaseAccessDeniedError, ValueError, NotImplementedError) as exc:
        raise _http_error(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={"continuation_url": location},
        headers=_private_no_store_headers(),
    )


@router.get("/auth/citation", response_class=HTMLResponse)
def citation_landing() -> HTMLResponse:
    """Strip the URL fragment before posting the ticket to the same-origin start route."""
    # No ticket is interpolated into HTML. It exists only in the fragment and one hidden
    # form value after history.replaceState has removed it from the address bar.
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="referrer" content="no-referrer">
<title>Continue to citation</title></head>
<body><main><p>Preparing the protected citation...</p></main>
<form id="citation-start" method="post" action="{html.escape(_START_PATH)}">
<input id="citation-ticket" type="hidden" name="ticket" value=""></form>
<script>
const ticket = window.location.hash.slice(1);
history.replaceState(null, "", window.location.pathname + window.location.search);
if (ticket) {{
  document.getElementById("citation-ticket").value = ticket;
  document.getElementById("citation-start").submit();
}} else {{
  document.querySelector("main").textContent = "This citation link is missing or expired.";
}}
</script></body></html>"""
    return HTMLResponse(document, headers=_private_no_store_headers())


@router.post("/auth/citation/start")
def start_citation_login(
    ticket: Annotated[str, Form(min_length=22, max_length=512)],
) -> RedirectResponse:
    """Consume the plaintext ticket once and start ordinary Mode 6 OIDC + PKCE."""
    settings = deps.get_settings()
    from .auth import (
        _client_secret,
        _require_oidc_session,
        _select_issuer,
        _validated_discovery,
        oidc_authorization_redirect,
    )

    _require_oidc_session(settings)
    auth_transaction_id = secrets.token_urlsafe(24)
    try:
        pending = deps.get_container().browser_flow_store.begin_citation(
            ticket,
            auth_transaction_id=auth_transaction_id,
            as_of=datetime.now(UTC),
        )
    except (BrowserFlowError, ValueError, NotImplementedError) as exc:
        raise _http_error(status.HTTP_400_BAD_REQUEST, "citation ticket is invalid") from exc
    issuer = _select_issuer(settings.identity, pending.registration.tenant)
    _client_secret(issuer)
    discovery = _validated_discovery(issuer)
    return oidc_authorization_redirect(
        settings,
        issuer,
        discovery,
        return_to="/",
        additional_transaction_claims={
            "citation_record_id": pending.record_id,
            "citation_installation_id": pending.registration.installation_id,
            "citation_auth_transaction_id": auth_transaction_id,
        },
    )


def complete_citation_callback(
    settings: Settings,
    txn_claims: dict[str, Any],
    principal: Principal,
) -> CitationContinuationRecord | None:
    """Consume and reauthorize a callback-bound citation, or return None for normal login."""
    record_id = str(txn_claims.get("citation_record_id") or "")
    if not record_id:
        return None
    installation_id = str(txn_claims.get("citation_installation_id") or "")
    transaction_id = str(txn_claims.get("citation_auth_transaction_id") or "")
    if not installation_id or not transaction_id:
        raise _http_error(
            status.HTTP_400_BAD_REQUEST,
            "citation authentication transaction is incomplete",
        )
    try:
        consumed = deps.get_container().browser_flow_store.consume_citation(
            record_id,
            actor=principal.actor,
            tenant=principal.tenant,
            installation_id=installation_id,
            auth_transaction_id=transaction_id,
            as_of=datetime.now(UTC),
        )
        _authorized_target(
            settings,
            principal,
            consumed.registration.citation_id,
            source_actor=consumed.registration.source_actor,
            target_origin=settings.channel.public_origin,
        )
    except BrowserFlowBindingError as exc:
        raise _http_error(
            status.HTTP_403_FORBIDDEN,
            "citation callback identity does not match",
        ) from exc
    except (BrowserFlowError, CaseAccessDeniedError, ValueError, NotImplementedError) as exc:
        raise _http_error(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    return consumed


def citation_confirmation_response(session: str, settings: Settings) -> HTMLResponse:
    """Render an agent-owned confirmation with no target URL in markup or JavaScript."""
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="referrer" content="no-referrer">
<title>Confirm citation</title></head><body><main>
<h1>Open protected source?</h1>
<p>Your identity and current evidence access have been verified.</p>
<form method="post" action="{html.escape(_CONTINUE_PATH)}">
<button type="submit">Open original source</button></form>
</main></body></html>"""
    response = HTMLResponse(document, headers=_private_no_store_headers())
    response.set_cookie(
        session_token.SESSION_COOKIE_NAME,
        session,
        max_age=settings.identity.session_ttl_seconds,
        httponly=True,
        secure=True,
        samesite="strict",
        path="/",
    )
    return response


@router.post("/auth/citation/continue")
def continue_to_citation(
    request: Request,
    context: CurrentAuthenticatedContext,
) -> RedirectResponse:
    """Reauthorize once more and navigate to the server-held HTTPS original."""
    settings = deps.get_settings()
    raw_txn = request.cookies.get(session_token.TXN_COOKIE_NAME, "")
    if not raw_txn:
        raise _http_error(status.HTTP_400_BAD_REQUEST, "citation transaction is missing")
    try:
        claims = session_token.verify(
            raw_txn,
            typ="txn",
            signing_key_env=settings.identity.session_signing_key_env,
            accepted_key_envs=settings.identity.session_accepted_key_envs,
        )
        record_id = str(claims.get("citation_record_id") or "")
        transaction_id = str(claims.get("citation_auth_transaction_id") or "")
        record = deps.get_container().browser_flow_store.get(record_id)
        if not isinstance(record, CitationContinuationRecord):
            raise ValueError("citation record kind does not match")
        if (
            record.state is not BrowserFlowState.CONSUMED
            or record.auth_transaction_id != transaction_id
            or record.registration.expected_actor != context.principal.actor
            or record.registration.tenant != context.principal.tenant
        ):
            raise BrowserFlowBindingError("citation confirmation binding does not match")
        target = _authorized_target(
            settings,
            context.principal,
            record.registration.citation_id,
            source_actor=record.registration.source_actor,
            target_origin=settings.channel.public_origin,
        )
    except (BrowserFlowError, CaseAccessDeniedError, ValueError, NotImplementedError) as exc:
        raise _http_error(status.HTTP_403_FORBIDDEN, "citation confirmation failed") from exc
    response = RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)
    response.headers.update(_private_no_store_headers())
    response.delete_cookie(
        session_token.TXN_COOKIE_NAME,
        path=f"{settings.channel.public_mount_path}/auth",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    return response
