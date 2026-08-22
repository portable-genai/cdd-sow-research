"""FastAPI application for the B1 CDD + Source-of-Wealth Agent.

Exposes the dossier endpoints (full CDD case, source-of-wealth narrative, adverse-media
scan) plus health and the A2A AgentCard at ``/.well-known/agent-card.json``. The
React/Next.js UI and the CLI consume this surface.

Design constraints:

* **Import-safe.** Building the :class:`~cdd_sow_research.config.Container` is deferred to
  request time via the ``deps`` factories, so importing this module (or ``app``) never
  touches Google Cloud. The on-prem/test profile imports it with no GCP SDK installed.
* **Guardrail blocks are not 500s.** A :class:`GuardrailBlockedError` from a service is
  translated to an HTTP 200 carrying an explicit blocked envelope flagged for human
  review, never a 500.
* **Region selected at deploy time**, defaulting to ``us-central1`` (SPEC §2).

Run locally with ``python -m cdd_sow_research.api.app`` (uvicorn on :8090).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import mimetypes
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse
from hex_service_kit.netdefaults import (
    ConfiguredEmptyError,
    InsecureBindError,
    resolve_bind_host,
)
from hex_service_kit.web import add_loopback_exposure_guard
from starlette.middleware.base import BaseHTTPMiddleware

from ..config import (
    UNCONSENTED_IDENTITY_MODE,
    Settings,
    build_container,
    end_user_auth_kind,
)
from ..domain import _grounded as g
from ..domain import case_bundle_service, entitlements
from ..domain import models as m
from ..domain.browser_flow import BrowserFlowError
from ..domain.errors import (
    CaseAccessDeniedError,
    CaseBundleError,
    CaseNotFoundError,
    ConcurrencyError,
    DocumentConflictError,
    DocumentNotFoundError,
    FourEyesError,
    GuardrailBlockedError,
    InvalidTransitionError,
    RetrievalEmptyError,
)
from ..domain.serialization import to_jsonable
from ..domain.services import CddService, PerpetualKycService, SowCaseService, UboGraphService
from ..envread import boolean_setting, optional_setting, setting_or_default
from ..ports.identity import VERIFIED
from . import deps
from .auth import router as auth_router
from .browser_flow_outbox import BrowserFlowOutboxDispatcher
from .citation_continuation import (
    record_cdd_citations,
)
from .citation_continuation import (
    router as citation_continuation_router,
)
from .csrf import enforce_cookie_csrf
from .schemas import (
    AddEvidenceRequest,
    AdverseMediaRequest,
    AdverseMediaResponse,
    AgentCardModel,
    CapabilityManifestModel,
    CapabilityModel,
    CaseBundleRestoreResponse,
    CddCaseResponse,
    CddRequest,
    DocumentListResponse,
    HealthResponse,
    OpenSowCaseRequest,
    OwnershipGraphResponse,
    PerpetualKycQueueResponse,
    PerpetualKycRequest,
    PerpetualKycResponse,
    PortableDossierArtifact,
    ReviewSowCaseRequest,
    SourceOfWealthResponse,
    StoredDocumentModel,
    SubjectRequest,
    UboGraphRequest,
    UboGraphResponse,
)
from .security import (
    BundleExportPrincipal,
    BundleImportPrincipal,
    CddReadPrincipal,
    CddWritePrincipal,
    DocumentsReadPrincipal,
    DocumentsWritePrincipal,
)

_DEV_ORIGINS = (
    "http://localhost:3000",
    "http://127.0.0.1:3000",
)


def _security_profile(settings: Settings) -> str:
    """The BIND posture, a restriction, so it reads the raw mode on purpose.

    ``resolve_bind_host`` confines ``local`` to loopback and lets fronted profiles take
    ``0.0.0.0``, so a run that chose nothing must look local here and stay confined. The
    relaxations below fail closed in the opposite direction and read
    ``exposure_identity_mode`` instead.
    """
    return "local" if settings.identity_mode == "local-persona" else "secure"


def _end_user_authenticated(settings: Settings) -> bool:
    """Does a request arrive with anything authenticating the END USER? Fails closed.

    Both halves must hold, and the exposure guard bounds every case where either fails:

      1. an identity mode was CHOSEN. Absent that, nobody selected an identity scheme, the
         seeded persona adapter refuses to construct, and every end-user route answers 401;
         but /healthz and the agent card would still answer a stranger, and a deployment in
         that state has no business being reachable at all. It is also the one case where a
         settings file that bound a verifying adapter must NOT buy the relaxation: unset is
         not consent, whatever the binding says;
      2. the identity adapter the active binding names DECLARES that it verifies the end
         user. Seeded personas arrive on the X-Dev-Persona header the caller wrote
         (client-asserted) and the on-premises placeholder resolves nobody at all
         (unimplemented); neither authenticates anyone, so neither may switch this off.

    Note what is NOT in this expression: CDD_S2S_TOKEN or any other service credential. A
    service credential is evidence about a calling SERVICE and says nothing about the
    end-user routes, so setting one must not, and cannot, disable their bound. S2S routes are
    bounded by their own dependency, which is where a service credential belongs.
    """
    return settings.identity_mode_explicit and end_user_auth_kind(settings) == VERIFIED


def _exposure_posture(settings: Settings) -> str:
    """The name the refusal message uses, resolved without ever raising.

    ``exposure_identity_mode`` is the repo's relaxation vocabulary, so an unconsented run
    already names itself ``unconfigured`` rather than borrowing the name of a mode an
    operator never chose. It reads ``identity_mode``, which RAISES for a runtime profile that
    infers no mode; that is a deployment nobody configured, which is exactly the case the
    guard is for, so it resolves to the same unconsented name instead of breaking the import.
    """
    try:
        return settings.exposure_identity_mode
    except ValueError:
        return UNCONSENTED_IDENTITY_MODE


def _bind_profile(settings: Settings) -> str:
    """The bind posture `main()` uses, widened by the same rule the request guard applies.

    ``_security_profile`` already reads an unconsented run as ``local``; this widens it to
    every posture that cannot authenticate an end user, so the start-up bound and the
    request-time guard agree instead of one binding every interface while the other refuses
    every caller on it.
    """
    return _security_profile(settings) if _end_user_authenticated(settings) else "local"


def _max_body_bytes(settings: Settings) -> int:
    return settings.web.max_body_bytes or (32 if settings.profile == "live" else 2) * 1024 * 1024


def _rate_per_minute(settings: Settings) -> int:
    if settings.web.rate_limit_per_minute >= 0:
        return settings.web.rate_limit_per_minute
    return 0 if settings.exposure_identity_mode == "local-persona" else 120


def _cors_origins(settings: Settings) -> tuple[str, ...]:
    """The CORS allowlist: configured wins, and a configured EMPTY allowlist refuses.

    The localhost dev fallback is a relaxation, so it applies only when the allowlist was
    never configured AND the no-auth persona mode was deliberately chosen.
    """
    if settings.web.cors_origins_configured:
        return settings.web.cors_origins
    return _DEV_ORIGINS if settings.exposure_identity_mode == "local-persona" else ()


_LOG = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Validate all independent selectors and exact adapter maps before serving."""
    configured_settings = getattr(_app.state, "configured_settings", None)
    settings = configured_settings or deps.get_settings()
    try:
        settings.validate_deployment()
    except (KeyError, ValueError) as exc:
        raise RuntimeError(f"invalid deployment configuration: {exc}") from exc
    previous_active_settings = getattr(_app.state, "active_settings", None)
    _app.state.active_settings = settings
    _LOG.info(
        "deployment selectors runtime=%s identity=%s channel=%s",
        settings.profile,
        settings.identity_mode,
        settings.channel_mode,
    )
    container = (
        build_container(settings) if configured_settings is not None else deps.get_container()
    )
    previous_active_container = getattr(_app.state, "active_container", None)
    _app.state.active_container = container
    first_dynamic_route = len(_app.router.routes)
    if settings.identity_mode == "embedded-grant":
        from .embed import create_embed_router
        from .embed_composition import build_embed_broker_dependencies

        _app.include_router(create_embed_router(build_embed_broker_dependencies(container)))
        _app.openapi_schema = None
    outbox_stop = asyncio.Event()
    outbox_task: asyncio.Task[None] | None = None
    browser_flow_binding = settings.adapters["browser_flow_store"][settings.profile]
    outbox_supported = (
        "disabled.browser_flow_store" not in browser_flow_binding
        and "onprem.browser_flow_store" not in browser_flow_binding
        and (settings.profile != "local" or bool(settings.local.browser_flow_path))
        and (settings.channel_mode == "sandboxed" or settings.identity_mode == "oidc-session")
    )
    if outbox_supported:
        dispatcher = BrowserFlowOutboxDispatcher(container.browser_flow_store, container.audit)
        outbox_task = asyncio.create_task(
            dispatcher.run(outbox_stop),
            name="browser-flow-audit-outbox",
        )
    try:
        yield
    finally:
        outbox_stop.set()
        if outbox_task is not None:
            try:
                await asyncio.wait_for(outbox_task, timeout=15.0)
            except TimeoutError:
                outbox_task.cancel()
                await asyncio.gather(outbox_task, return_exceptions=True)
        if len(_app.router.routes) > first_dynamic_route:
            del _app.router.routes[first_dynamic_route:]
            _app.openapi_schema = None
        if previous_active_settings is None:
            delattr(_app.state, "active_settings")
        else:
            _app.state.active_settings = previous_active_settings
        if previous_active_container is None:
            delattr(_app.state, "active_container")
        else:
            _app.state.active_container = previous_active_container


api_router = APIRouter()


class _TokenBucket:
    """Tiny in-process per-key rate limiter (backstop, not the edge policy)."""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        if self.per_minute <= 0:
            return True
        now = time.monotonic()
        with self._lock:
            window = [t for t in self._hits.get(key, ()) if now - t < 60.0]
            if len(window) >= self.per_minute:
                self._hits[key] = window
                return False
            window.append(now)
            self._hits[key] = window
            return True


class _DeploymentSecurityMiddleware(BaseHTTPMiddleware):
    """Apply CORS, CSRF, limits, and headers from one validated Settings instance."""

    def __init__(self, application: Any, *, configured_settings: Settings | None) -> None:
        super().__init__(application)
        self._configured_settings = configured_settings
        self._buckets: dict[tuple[str, bool], _TokenBucket] = {}
        self._bucket_lock = threading.Lock()

    def _settings(self, request: Request) -> Settings:
        return (
            self._configured_settings
            or getattr(request.app.state, "active_settings", None)
            or deps.get_settings()
        )

    def _bucket(self, settings: Settings, *, auth: bool) -> _TokenBucket:
        rate = _rate_per_minute(settings)
        if auth and rate:
            rate = max(10, rate // 10)
        key = (settings.configuration_hash(), auth)
        with self._bucket_lock:
            return self._buckets.setdefault(key, _TokenBucket(rate))

    @staticmethod
    def _allowed_headers(settings: Settings) -> tuple[str, ...]:
        common = (
            "Content-Type",
            "Authorization",
            "X-Request-ID",
            "X-CSRF-Token",
            "X-CDD-Installation-ID",
            "X-CDD-Manifest-SHA256",
        )
        return common + (
            ("X-Dev-Persona",) if settings.exposure_identity_mode == "local-persona" else ()
        )

    @staticmethod
    def _security_headers(response: Response, settings: Settings) -> None:
        response.headers["Content-Security-Policy"] = "frame-ancestors " + " ".join(
            settings.web.frame_ancestors
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if settings.web.frame_ancestors == ("'self'",):
            response.headers["X-Frame-Options"] = "SAMEORIGIN"
        if _security_profile(settings) == "secure":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

    @staticmethod
    def _cors_headers(response: Response, request: Request, settings: Settings) -> None:
        origin = request.headers.get("origin", "")
        if origin and origin in _cors_origins(settings):
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers.add_vary_header("Origin")

    @classmethod
    def _finalize_response(
        cls,
        response: Response,
        request: Request,
        settings: Settings,
    ) -> Response:
        """Apply browser and security posture to every middleware exit path."""
        cls._cors_headers(response, request, settings)
        cls._security_headers(response, settings)
        return response

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        container = getattr(request.app.state, "active_container", None)
        token = deps.bind_request_container(container) if container is not None else None
        try:
            return await self._dispatch(request, call_next)
        finally:
            if token is not None:
                deps.reset_request_container(token)

    async def _dispatch(self, request: Request, call_next: Any) -> Response:
        settings = self._settings(request)
        origin = request.headers.get("origin", "")
        if request.method == "OPTIONS" and request.headers.get("access-control-request-method"):
            requested_method = request.headers["access-control-request-method"].upper()
            requested_headers = {
                item.strip().lower()
                for item in request.headers.get("access-control-request-headers", "").split(",")
                if item.strip()
            }
            allowed_headers = {item.lower() for item in self._allowed_headers(settings)}
            response: Response
            if (
                origin not in _cors_origins(settings)
                or requested_method not in {"GET", "POST", "DELETE", "OPTIONS"}
                or not requested_headers.issubset(allowed_headers)
            ):
                response = JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content={"detail": "disallowed CORS preflight"},
                )
            else:
                response = Response(
                    status_code=status.HTTP_200_OK,
                    headers={
                        "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
                        "Access-Control-Allow-Headers": ", ".join(self._allowed_headers(settings)),
                        "Access-Control-Max-Age": "600",
                    },
                )
            return self._finalize_response(response, request, settings)

        if (
            settings.channel_mode == "sandboxed"
            and request.url.path.startswith("/v1/")
            and request.url.path != "/v1/embed/grants"
        ):
            supplied_digest = request.headers.get("x-cdd-manifest-sha256", "")
            try:
                expected_digest = settings.installation_manifest().sha256
            except ValueError:
                response = JSONResponse(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    content={"detail": "installation manifest binding is unavailable"},
                )
                return self._finalize_response(response, request, settings)
            if len(supplied_digest) != 64 or not hmac.compare_digest(
                supplied_digest,
                expected_digest,
            ):
                response = JSONResponse(
                    status_code=status.HTTP_409_CONFLICT,
                    content={"detail": "installation manifest binding does not match"},
                )
                return self._finalize_response(response, request, settings)

        try:
            enforce_cookie_csrf(request, settings)
        except HTTPException as exc:
            response = JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
            return self._finalize_response(response, request, settings)

        max_body_bytes = _max_body_bytes(settings)
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > max_body_bytes:
            response = JSONResponse(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                content={"detail": f"request body exceeds {max_body_bytes} bytes"},
            )
            return self._finalize_response(response, request, settings)
        client = request.client.host if request.client else "unknown"
        is_auth = request.url.path.startswith(("/auth/", "/agent/auth/"))
        if not self._bucket(settings, auth=is_auth).allow(client):
            response = JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={"detail": "rate limit exceeded"},
                headers={"Retry-After": "60"},
            )
            return self._finalize_response(response, request, settings)
        response = await call_next(request)
        return self._finalize_response(response, request, settings)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Construct an import-safe API whose middleware shares one deployment posture."""
    application = FastAPI(
        lifespan=_lifespan,
        title="B1 CDD + Source-of-Wealth Agent",
        version="0.2.0",
        description=(
            "Grounded research agent that turns a customer's KYC pack, corporate registries "
            "and adverse media into a cited CDD dossier (source-of-wealth narrative, risk "
            "rating, adverse-media findings, and a UBO summary), with a full audit trail, on "
            "the Gemini Enterprise Agent Platform. Region is configurable and defaults to "
            "us-central1."
        ),
    )
    application.state.configured_settings = settings
    application.add_middleware(
        _DeploymentSecurityMiddleware,
        configured_settings=settings,
    )
    # Registered LAST, so it is the OUTERMOST middleware: an off-loopback caller is refused
    # before the deployment-security middleware (CORS, CSRF, limits, headers) and before any
    # route or dependency runs. Bound to the APP OBJECT, not to `main()`: the Dockerfile CMD
    # is `uvicorn cdd_sow_research.api.app:app --host 0.0.0.0 --port ${PORT}`, so a guard
    # reachable only from `main()` never runs in a shipped process and the seeded personas
    # would be served to the LAN, whole: subjects, tenants and group memberships, with a LAN
    # peer then free to act as any of them by naming one in a header.
    posture_settings = settings or deps.get_settings()
    add_loopback_exposure_guard(
        application,
        unauthenticated=not _end_user_authenticated(posture_settings),
        insecure_demo_env="CDD_ALLOW_INSECURE_DEMO",
        posture=_exposure_posture(posture_settings),
    )
    application.include_router(auth_router)
    application.include_router(citation_continuation_router)
    application.include_router(api_router)
    return application


def _blocked_response(detail: str, reason: str) -> JSONResponse:
    """A 200 JSON body for a guardrail-blocked request (flagged for human review)."""
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "blocked": True,
            "requires_human_review": True,
            "detail": detail,
            "reason": reason or "blocked",
        },
    )


def _denied_response(exc: CaseAccessDeniedError) -> JSONResponse:
    """403 for a failed server-side case entitlement check (never a data leak)."""
    return JSONResponse(
        status_code=status.HTTP_403_FORBIDDEN,
        content={"detail": str(exc)},
    )


# --------------------------------------------------------------------------- #
# Case documents (custody of the evidence a dossier cites)
#
# The case id is in the path, not just the document id, so the reader's ACL is derived
# from the VERIFIED principal before the store is touched. The store then re-checks the
# document's own tags as a subset of those principals, so a document belonging to
# another case or tenant is invisible even to a caller entitled to some case.
# --------------------------------------------------------------------------- #
def _document_not_found(document_id: str) -> JSONResponse:
    """404 for absent AND unreadable alike, so document ids cannot be probed."""
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={"detail": f"no readable document {document_id!r}"},
    )


@api_router.post(
    "/v1/cases/{case_id}/documents",
    response_model=StoredDocumentModel,
    status_code=status.HTTP_201_CREATED,
    tags=["documents"],
)
async def upload_case_document(
    case_id: str,
    principal: DocumentsWritePrincipal,
    file: Annotated[UploadFile, File(description="The document to place in case custody")],
    doc_type: Annotated[str, Form()] = "other",
) -> JSONResponse | StoredDocumentModel:
    """Place an uploaded KYC document in custody for a case.

    The bytes are stored under the case's ACL tags (derived server-side), never a
    client-supplied one, and are read back later by the assessment pipeline and by the
    citation links in the finished dossier.
    """
    try:
        entitlements.case_scope(principal, case_id)
    except CaseAccessDeniedError as exc:
        return _denied_response(exc)

    container = deps.get_container()
    limits = container.settings.document_store
    # Read to the ceiling plus one byte and stop. Reading the whole upload first and
    # measuring it afterwards means an oversized body is fully buffered in memory before
    # being rejected, and the request-level cap cannot be relied on to prevent that: it
    # is enforced from Content-Length, which a chunked upload simply does not send.
    content = await _read_capped(file, limits.max_upload_bytes)
    if content is None:
        return JSONResponse(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            content={
                "detail": (
                    f"document exceeds the per-document limit of {limits.max_upload_bytes} bytes"
                )
            },
        )
    if not content:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": "uploaded file is empty"},
        )
    mime_type = _resolve_mime(file)
    if mime_type not in limits.allowed_mime_types:
        return JSONResponse(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            content={
                "detail": (
                    f"cannot read {mime_type!r}; supported types are "
                    f"{', '.join(limits.allowed_mime_types)}"
                )
            },
        )
    try:
        kind = m.DocType(doc_type)
    except ValueError:
        kind = m.DocType.OTHER

    record = container.document_store.put(
        content=content,
        filename=file.filename or "document",
        doc_type=kind,
        subject_id=case_id,
        acl_tags=entitlements.case_tags(case_id, principal.tenant),
        mime_type=mime_type,
    )
    return StoredDocumentModel.from_domain(record)


@api_router.get(
    "/v1/cases/{case_id}/documents",
    response_model=DocumentListResponse,
    tags=["documents"],
)
def list_case_documents(
    case_id: str, principal: DocumentsReadPrincipal
) -> JSONResponse | DocumentListResponse:
    """List the documents held in custody for a case (metadata only, newest first)."""
    try:
        scope = entitlements.case_scope(principal, case_id)
    except CaseAccessDeniedError as exc:
        return _denied_response(exc)
    records = deps.get_container().document_store.list_documents(scope, subject_id=case_id)
    return DocumentListResponse(documents=[StoredDocumentModel.from_domain(r) for r in records])


@api_router.get(
    "/v1/cases/{case_id}/documents/{document_id}",
    response_model=None,
    tags=["documents"],
)
def get_case_document(
    case_id: str, document_id: str, principal: DocumentsReadPrincipal
) -> JSONResponse | Response:
    """Serve a stored document's original bytes: the target of every dossier citation.

    Rendered inline so a reviewer following a citation lands in the document itself
    (the UI appends ``#page=N`` to open the cited page).
    """
    try:
        scope = entitlements.case_scope(principal, case_id)
    except CaseAccessDeniedError as exc:
        return _denied_response(exc)
    store = deps.get_container().document_store
    try:
        record = store.metadata(document_id, scope)
        content = store.get(document_id, scope)
    except DocumentNotFoundError:
        return _document_not_found(document_id)
    return Response(
        content=content,
        media_type=record.mime_type or "application/octet-stream",
        headers={
            # inline: the reviewer reads it in place. The filename is quoted and the
            # header is built from the stored record, never from the request.
            "Content-Disposition": f'inline; filename="{_safe_filename(record.filename)}"',
            "Cache-Control": "private, no-store",
        },
    )


@api_router.delete("/v1/cases/{case_id}/documents/{document_id}", tags=["documents"])
def delete_case_document(
    case_id: str, document_id: str, principal: DocumentsWritePrincipal
) -> JSONResponse:
    """Remove a document from a case's custody (before an assessment is run)."""
    try:
        scope = entitlements.case_scope(principal, case_id)
    except CaseAccessDeniedError as exc:
        return _denied_response(exc)
    try:
        deleted = deps.get_container().document_store.delete(document_id, scope)
    except DocumentNotFoundError:
        return _document_not_found(document_id)
    if not deleted:
        return _document_not_found(document_id)
    return JSONResponse(content={"deleted": document_id})


async def _read_capped(file: UploadFile, limit: int) -> bytes | None:
    """Read at most ``limit`` bytes, or None when the upload is larger than that.

    Stops at the first chunk that crosses the ceiling, so an oversized upload is refused
    without ever being held whole in memory.
    """
    chunk_size = 1024 * 1024
    buffer = bytearray()
    while True:
        chunk = await file.read(chunk_size)
        if not chunk:
            return bytes(buffer)
        buffer.extend(chunk)
        if len(buffer) > limit:
            return None


def _resolve_mime(file: UploadFile) -> str:
    """The upload's media type: the browser's value, else guessed from the filename."""
    declared = (file.content_type or "").split(";")[0].strip().lower()
    if declared and declared != "application/octet-stream":
        return "image/jpeg" if declared == "image/jpg" else declared
    guessed, _ = mimetypes.guess_type(file.filename or "")
    return (guessed or "application/octet-stream").lower()


def _safe_filename(filename: str) -> str:
    """A filename safe to place in a header: no quotes, no newlines, never empty."""
    cleaned = "".join(c for c in (filename or "") if c.isprintable() and c not in '"\\')
    return cleaned[:120] or "document"


# --------------------------------------------------------------------------- #
# Artifact endpoints
# --------------------------------------------------------------------------- #
@api_router.post("/v1/cdd", response_model=CddCaseResponse, tags=["artifacts"])
def assess_cdd(
    request: CddRequest,
    principal: CddWritePrincipal,
    service: Annotated[CddService, Depends(deps.get_cdd_service)],
) -> JSONResponse | CddCaseResponse:
    """Build a full cited CDD dossier for a subject and its KYC documents."""
    # Object-level authorization: the case ACL principal is granted server-side from
    # the VERIFIED principal, never from the request body's subject id alone.
    try:
        entitlements.case_scope(principal, request.subject.id)
    except CaseAccessDeniedError as exc:
        return _denied_response(exc)
    try:
        case = service.assess(
            request.to_case_input(),
            actor=principal.actor,
            principals=principal.principals,
            tenant=principal.tenant,
        )
    except GuardrailBlockedError as exc:
        return _blocked_response(
            "This CDD request was blocked by the safety guardrail and routed for human review.",
            str(exc),
        )
    except RetrievalEmptyError as exc:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": f"No case evidence available to ground the dossier: {exc}"},
        )
    continuation_ids: frozenset[str] = frozenset()
    settings = deps.get_settings()
    if settings.channel_mode == "sandboxed" and settings.identity_mode in {
        "oauth-access-token",
        "embedded-grant",
    }:
        try:
            continuation_ids = record_cdd_citations(case, principal)
        except (BrowserFlowError, NotImplementedError, ValueError) as exc:
            _LOG.error("citation ledger persistence failed: %s", type(exc).__name__)
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"detail": "citation continuation ledger is unavailable"},
            )
    return CddCaseResponse.from_domain(case, continuation_ids)


def _dossier_digest(dossier: CddCaseResponse) -> str:
    encoded = json.dumps(
        dossier.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


@api_router.post(
    "/v1/portable/dossiers/export",
    response_model=PortableDossierArtifact,
    tags=["artifacts"],
)
def export_portable_dossier(
    dossier: CddCaseResponse,
    principal: CddReadPrincipal,
) -> JSONResponse | PortableDossierArtifact:
    """Package a dossier in an open, backend-neutral, integrity-protected envelope."""
    try:
        entitlements.case_scope(principal, dossier.subject.id)
    except CaseAccessDeniedError as exc:
        return _denied_response(exc)
    return PortableDossierArtifact(
        sha256=_dossier_digest(dossier),
        exported_at=datetime.now(UTC).isoformat(),
        dossier=dossier,
    )


@api_router.post(
    "/v1/portable/dossiers/import",
    response_model=CddCaseResponse,
    tags=["artifacts"],
)
def import_portable_dossier(
    artifact: PortableDossierArtifact,
    principal: CddReadPrincipal,
) -> JSONResponse | CddCaseResponse:
    """Validate and reload a portable dossier without selecting a storage vendor."""
    try:
        entitlements.case_scope(principal, artifact.dossier.subject.id)
    except CaseAccessDeniedError as exc:
        return _denied_response(exc)
    expected = _dossier_digest(artifact.dossier)
    if not hmac.compare_digest(expected, artifact.sha256):
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": "portable dossier integrity check failed"},
        )
    return artifact.dossier


@api_router.post(
    "/v1/cases/{case_id}/bundle/export",
    response_model=None,
    tags=["artifacts"],
)
def export_case_bundle(
    case_id: str,
    dossier: CddCaseResponse,
    principal: BundleExportPrincipal,
) -> JSONResponse | Response:
    """Export the dossier AND every source document the caller may read, as one archive.

    The portable dossier envelope proves the case travels; this proves its EVIDENCE
    travels with it. The response is a ZIP (manifest, dossier, original document bytes),
    so the receiving side needs no tooling from this vendor to open it.

    The manifest digest is returned in the ``X-Bundle-Manifest-Sha256`` header rather
    than only inside the archive: carried out of band, that value is what turns the
    bundle's internal digests from a corruption check into a tamper-evident one.
    """
    try:
        scope = entitlements.case_scope(principal, case_id)
    except CaseAccessDeniedError as exc:
        return _denied_response(exc)
    if dossier.subject.id != case_id:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": f"dossier describes {dossier.subject.id!r}, not case {case_id!r}"},
        )
    exported = case_bundle_service.export_bundle(
        deps.get_container().document_store,
        case_id=case_id,
        dossier=dossier.model_dump(mode="json"),
        scope=scope,
        exported_at=datetime.now(UTC).isoformat(),
    )
    return Response(
        content=exported.content,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{exported.filename}"',
            "X-Bundle-Manifest-Sha256": exported.manifest_sha256,
            "Cache-Control": "private, no-store",
        },
    )


@api_router.post(
    "/v1/cases/{case_id}/bundle/import",
    response_model=CaseBundleRestoreResponse,
    tags=["artifacts"],
)
async def import_case_bundle(
    case_id: str,
    principal: BundleImportPrincipal,
    file: Annotated[UploadFile, File(description="The case bundle archive to reload")],
    manifest_sha256: Annotated[str, Form()] = "",
) -> JSONResponse | CaseBundleRestoreResponse:
    """Verify a case bundle and place its documents back in custody for ``case_id``.

    The bundle's own ACL tags are provenance only: every restored document is filed
    under tags derived here from the VERIFIED principal, so an archive edited to carry
    another tenant's tags gains nothing by it.

    Supply ``manifest_sha256`` when the exporting side recorded the manifest digest out
    of band; the reload then refuses a bundle whose manifest was rewritten in transit,
    which the in-archive digests alone cannot detect.
    """
    try:
        entitlements.case_scope(principal, case_id)
    except CaseAccessDeniedError as exc:
        return _denied_response(exc)

    container = deps.get_container()
    limits = container.settings.document_store
    data = await _read_capped(file, limits.max_bundle_bytes)
    if data is None:
        return JSONResponse(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            content={"detail": f"bundle exceeds the limit of {limits.max_bundle_bytes} bytes"},
        )
    try:
        restored = case_bundle_service.restore_bundle(
            container.document_store,
            data,
            case_id=case_id,
            acl_tags=entitlements.case_tags(case_id, principal.tenant),
            expected_manifest_sha256=manifest_sha256,
            max_total_bytes=limits.max_bundle_uncompressed_bytes,
            max_documents=limits.max_bundle_documents,
        )
    except CaseBundleError as exc:
        # One status for every rejection: the bundle is not trustworthy, and which
        # check caught it is detail for the operator, not a different outcome.
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": f"case bundle rejected: {exc}"},
        )
    except DocumentConflictError as exc:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": str(exc)},
        )
    except NotImplementedError as exc:
        return JSONResponse(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            content={"detail": str(exc)},
        )
    return CaseBundleRestoreResponse.from_domain(restored)


@api_router.post(
    "/v1/source-of-wealth",
    response_model=SourceOfWealthResponse,
    tags=["artifacts"],
)
def source_of_wealth(
    request: SubjectRequest, principal: CddReadPrincipal
) -> JSONResponse | SourceOfWealthResponse:
    """Build a cited source-of-wealth narrative for a subject (uses A2 governed retrieval)."""
    container = deps.get_container()
    subject = request.subject.to_domain()
    # The retrieval scope is derived entirely server-side from the verified principal
    # (entitlement check + tenant tag + case principal); a client-supplied subject id
    # alone cannot read another case's evidence.
    try:
        scope = entitlements.case_scope(principal, subject.id)
    except CaseAccessDeniedError as exc:
        return _denied_response(exc)
    passages = g.retrieve_passages(
        container.knowledge_base,
        f"source of wealth evidence for {subject.name}",
        acl_principals=scope,
        top_k=container.settings.knowledge_base.top_k,
    )
    service = deps.build_sow_service(container)
    sow = service.build(subject, passages, principal.actor)
    return SourceOfWealthResponse.from_domain(sow)


@api_router.post(
    "/v1/adverse-media",
    response_model=AdverseMediaResponse,
    tags=["artifacts"],
)
def adverse_media(
    request: AdverseMediaRequest, principal: CddReadPrincipal
) -> AdverseMediaResponse:
    """Scan public-web adverse media for a subject name."""
    container = deps.get_container()
    service = deps.build_adverse_media_service(container)
    from ..domain.models import Subject

    screening = service.scan(Subject(id="adhoc", name=request.subject_name), principal.actor)
    return AdverseMediaResponse.from_domain(request.subject_name, screening)


# --------------------------------------------------------------------------- #
# Perpetual KYC (MonitoringStorePort; ACL derived server-side, routed to Hrz7)
# --------------------------------------------------------------------------- #
def _as_of(value: str) -> date:
    """Parse the optional replay date, defaulting to today (UTC)."""
    if not value:
        return datetime.now(UTC).date()
    return date.fromisoformat(value[:10])


@api_router.post("/v1/perpetual-kyc", response_model=PerpetualKycResponse, tags=["monitoring"])
def run_perpetual_kyc(
    request: PerpetualKycRequest,
    principal: CddWritePrincipal,
    service: Annotated[PerpetualKycService, Depends(deps.get_perpetual_kyc_service)],
) -> JSONResponse | PerpetualKycResponse:
    """Run one perpetual-KYC cycle: detect change, re-score, queue for human review.

    The re-score is deterministic pure code and the outcome always requires human review;
    the assessment is routed to Hrz7 and never acted on here. The subject's tenant is
    stamped from the VERIFIED principal, so a monitoring record cannot be planted in (or
    read from) another tenant's ACL: a cross-tenant caller gets 403, not 404.
    """
    from dataclasses import replace

    try:
        scope = entitlements.case_scope(principal, request.subject.id)
    except CaseAccessDeniedError as exc:
        return _denied_response(exc)
    try:
        as_of = _as_of(request.as_of)
    except ValueError:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": "as_of must be an ISO date (YYYY-MM-DD)"},
        )
    subject = replace(request.subject.to_domain(), tenant=principal.tenant)
    try:
        assessment = service.run(
            subject,
            actor=principal.actor,
            principals=scope,
            as_of=as_of,
            last_reviewed=request.last_reviewed,
        )
    except CaseAccessDeniedError as exc:
        return _denied_response(exc)
    return PerpetualKycResponse.from_domain(assessment)


@api_router.get(
    "/v1/perpetual-kyc/queue",
    response_model=PerpetualKycQueueResponse,
    tags=["monitoring"],
)
def perpetual_kyc_queue(
    principal: CddReadPrincipal,
    service: Annotated[PerpetualKycService, Depends(deps.get_perpetual_kyc_service)],
) -> JSONResponse | PerpetualKycQueueResponse:
    """The caller's explainable perpetual-KYC review queue, most urgent first.

    The listing scope is derived entirely server-side from the verified principal: a
    case-access role plus the principal's own tenant tag. A record carrying no tenant tag,
    or another tenant's, is never listed (fail-closed in both directions).
    """
    try:
        scope = entitlements.queue_scope(principal)
    except CaseAccessDeniedError as exc:
        return _denied_response(exc)
    return PerpetualKycQueueResponse.from_domain(service.queue(scope))


# --------------------------------------------------------------------------- #
# UBO graph (OwnershipGraphPort; ACL derived server-side, routed to Hrz7)
#
# Two verbs with deliberately different consequences. POST resolves: it produces the
# findings, the control basis and the indicators, which is a consequential claim, so it
# always requires human review and is routed to Hrz7 under rule R8. GET returns the WALKED
# STRUCTURE ONLY (layers, edges, citations) with no finding, no basis and no flag, so it
# is evidence rather than a decision and stays a side-effect-free read. See
# docs/ubo-graph-contract.md for why the read was drawn there and not elsewhere.
# --------------------------------------------------------------------------- #
@api_router.post("/v1/ubo-graph", response_model=UboGraphResponse, tags=["ownership"])
def resolve_ubo_graph(
    request: UboGraphRequest,
    principal: CddWritePrincipal,
    service: Annotated[UboGraphService, Depends(deps.get_ubo_graph_service)],
) -> JSONResponse | UboGraphResponse:
    """Resolve a subject's cross-jurisdiction beneficial-ownership structure.

    Every percentage is the deterministic product of the cited registry hops, computed by
    pure code an auditor can recompute; the outcome always requires human review and is
    routed to Hrz7 rather than acted on. The subject's tenant is stamped from the VERIFIED
    principal, so a resolution can never be routed under another tenant's ACL: a caller
    with no case entitlement gets 403.
    """
    from dataclasses import replace

    try:
        entitlements.case_scope(principal, request.subject.id)
    except CaseAccessDeniedError as exc:
        return _denied_response(exc)
    try:
        as_of = _as_of(request.as_of)
    except ValueError:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": "as_of must be an ISO date (YYYY-MM-DD)"},
        )
    subject = replace(request.subject.to_domain(), tenant=principal.tenant)
    resolution = service.resolve(subject, actor=principal.actor, as_of=as_of)
    return UboGraphResponse.from_domain(resolution)


@api_router.get(
    "/v1/ubo-graph/{subject_id}",
    response_model=OwnershipGraphResponse,
    tags=["ownership"],
)
def ubo_graph_for_subject(
    subject_id: str,
    principal: CddReadPrincipal,
    service: Annotated[UboGraphService, Depends(deps.get_ubo_graph_service)],
    name: Annotated[str, Query(description="Registered entity name; defaults to the id.")] = "",
    jurisdiction: Annotated[str, Query(description="ISO-ish country code.")] = "",
    as_of: Annotated[str, Query(description="ISO date to evaluate for; today by default.")] = "",
) -> JSONResponse | OwnershipGraphResponse:
    """Fetch the walked ownership structure for a subject (evidence, not a verdict).

    Recomputed rather than read from a store: the graph is a pure function of the registry
    layers plus policy, so there is no store port and therefore no staler second answer.
    The subject is a corporate entity, so its registered name is not personal data; it is
    still optional and defaults to the subject id.
    """
    try:
        entitlements.case_scope(principal, subject_id)
    except CaseAccessDeniedError as exc:
        return _denied_response(exc)
    try:
        when = _as_of(as_of)
    except ValueError:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": "as_of must be an ISO date (YYYY-MM-DD)"},
        )
    from ..domain.models import Subject, SubjectType

    subject = Subject(
        id=subject_id,
        name=name or subject_id,
        type=SubjectType.ENTITY,
        jurisdiction=jurisdiction,
        tenant=principal.tenant,
    )
    graph = service.graph(subject, actor=principal.actor, as_of=when)
    return OwnershipGraphResponse.from_domain(graph)


# --------------------------------------------------------------------------- #
# Longitudinal SoW cases (managed CaseStorePort; ACL derived server-side)
# --------------------------------------------------------------------------- #
def _sow_error_response(exc: Exception) -> JSONResponse:
    """Map a SoW-case domain error to its HTTP status (never leak an unhandled 500)."""
    if isinstance(exc, CaseAccessDeniedError):
        return _denied_response(exc)
    if isinstance(exc, CaseNotFoundError):
        code = status.HTTP_404_NOT_FOUND
    elif isinstance(exc, (ConcurrencyError, InvalidTransitionError, FourEyesError)):
        code = status.HTTP_409_CONFLICT
    else:  # pragma: no cover - defensive
        raise exc
    return JSONResponse(status_code=code, content={"detail": str(exc)})


def _case_principals(principal: object, case_id: str) -> tuple[str, ...]:
    """The server-derived ACL principals for ``case_id`` (raises CaseAccessDeniedError)."""
    return entitlements.case_scope(principal, case_id)  # type: ignore[arg-type]


@api_router.post("/v1/sow-cases", tags=["sow-cases"])
def open_sow_case(
    request: OpenSowCaseRequest,
    principal: CddWritePrincipal,
    service: Annotated[SowCaseService, Depends(deps.get_sow_case_service)],
) -> JSONResponse:
    """Open a longitudinal SoW case. The case's tenant is stamped from the verified
    principal (never client-supplied), so its ACL isolates it to the caller's tenant."""
    from dataclasses import replace

    try:
        _case_principals(principal, request.case_id)
        subject = replace(request.subject.to_domain(), tenant=principal.tenant)
        case = service.open(request.case_id, subject, None, actor=principal.actor)
    except CaseAccessDeniedError as exc:
        return _sow_error_response(exc)
    return JSONResponse(status_code=status.HTTP_201_CREATED, content=to_jsonable(case))


@api_router.post("/v1/sow-cases/{case_id}/evidence", tags=["sow-cases"])
def add_sow_evidence(
    case_id: str,
    request: AddEvidenceRequest,
    principal: CddWritePrincipal,
    service: Annotated[SowCaseService, Depends(deps.get_sow_case_service)],
) -> JSONResponse:
    """Append a round of evidence to a case's open iteration."""
    try:
        principals = _case_principals(principal, case_id)
        items = [i.to_domain() for i in request.items]
        case = service.add_evidence(case_id, principals, items, actor=principal.actor)
    except Exception as exc:  # noqa: BLE001 - mapped to a status by _sow_error_response
        return _sow_error_response(exc)
    return JSONResponse(content=to_jsonable(case))


@api_router.post("/v1/sow-cases/{case_id}/analyze", tags=["sow-cases"])
def analyze_sow_case(
    case_id: str,
    principal: CddWritePrincipal,
    service: Annotated[SowCaseService, Depends(deps.get_sow_case_service)],
) -> JSONResponse:
    """Run one analysis round: reconcile, find gaps, draft RFIs, advance state."""
    try:
        principals = _case_principals(principal, case_id)
        case = service.analyze(case_id, principals, actor=principal.actor)
    except Exception as exc:  # noqa: BLE001 - mapped to a status by _sow_error_response
        return _sow_error_response(exc)
    return JSONResponse(content=to_jsonable(case))


@api_router.post("/v1/sow-cases/{case_id}/review", tags=["sow-cases"])
def review_sow_case(
    case_id: str,
    request: ReviewSowCaseRequest,
    principal: CddWritePrincipal,
    service: Annotated[SowCaseService, Depends(deps.get_sow_case_service)],
) -> JSONResponse:
    """Maker-checker disposition (four-eyes): approve seals a snapshot, else re-open."""
    try:
        principals = _case_principals(principal, case_id)
        case = service.review(case_id, principals, request.approve, checker=principal.actor)
    except Exception as exc:  # noqa: BLE001 - mapped to a status by _sow_error_response
        return _sow_error_response(exc)
    return JSONResponse(content=to_jsonable(case))


@api_router.get("/v1/sow-cases/{case_id}", tags=["sow-cases"])
def get_sow_case(
    case_id: str,
    principal: CddReadPrincipal,
    service: Annotated[SowCaseService, Depends(deps.get_sow_case_service)],
) -> JSONResponse:
    """Load a case (ACL enforced server-side: a cross-tenant caller gets 403)."""
    try:
        principals = _case_principals(principal, case_id)
        case = service.store.load(case_id, principals)
    except Exception as exc:  # noqa: BLE001 - mapped to a status by _sow_error_response
        return _sow_error_response(exc)
    return JSONResponse(content=to_jsonable(case))


# --------------------------------------------------------------------------- #
# Health & governance
# --------------------------------------------------------------------------- #
@api_router.get("/healthz", response_model=HealthResponse, tags=["ops"])
def healthz() -> HealthResponse:
    """Liveness/readiness plus safe, non-secret deployment-selector metadata."""
    settings = deps.get_settings()
    capabilities = _capability_manifest(settings)
    manifest = settings.installation_manifest() if settings.channel_mode == "sandboxed" else None
    return HealthResponse(
        status="ok",
        profile=settings.profile,
        region=settings.region,
        mode=settings.deprecated_control_ownership,
        identity_mode=settings.identity_mode,
        channel_mode=settings.channel_mode,
        manifest_version=settings.channel.manifest_version or "not-configured",
        deployment_manifest_id=(
            manifest.manifest.deployment_manifest_id if manifest else "not-configured"
        ),
        build_id=manifest.manifest.build_id if manifest else "not-configured",
        manifest_sha256=manifest.sha256 if manifest else "",
        configuration_hash=settings.configuration_hash(),
        demo_only=capabilities.demo_only,
        production_ready=capabilities.production_ready,
    )


@api_router.get("/v1/capabilities", response_model=CapabilityManifestModel, tags=["ops"])
def capabilities() -> CapabilityManifestModel:
    """Expose real adapter availability so the UI never fabricates managed assurance."""
    return _capability_manifest(deps.get_settings())


def _capability_manifest(settings: Settings) -> CapabilityManifestModel:
    demo_only = settings.profile in {"local", "live"}
    managed = settings.profile in {"gcp", "platform"}
    core_available = demo_only or managed
    refs = {
        "cdd-workflow": optional_setting("CDD_WORKFLOW_ATTESTATION_REF") or "",
        "evaluation-gate": optional_setting("CDD_EVALUATION_ATTESTATION_REF") or "",
        "immutable-audit": optional_setting("CDD_AUDIT_ATTESTATION_REF") or "",
        "observability": optional_setting("CDD_TRACE_ATTESTATION_REF") or "",
        "model-armor": optional_setting("CDD_MODEL_ARMOR_ATTESTATION_REF") or "",
        "embeddable-ui": optional_setting("CDD_UI_ATTESTATION_REF") or "",
    }

    def assurance(name: str) -> str:
        return "attested" if managed and refs[name] else "not-attested"

    items = [
        CapabilityModel(
            name="cdd-workflow",
            available=core_available,
            mode="local" if demo_only else ("managed" if managed else "disabled"),
            assurance=(
                "demo-only"
                if demo_only
                else (assurance("cdd-workflow") if managed else "unavailable")
            ),
            provider="portable domain core",
            reason=(
                "functional synthetic-data laptop workflow; output is not attested"
                if demo_only
                else ("" if managed else "production replacement is not configured")
            ),
            required_for_production=True,
        ),
        CapabilityModel(
            name="evaluation-gate",
            available=core_available,
            mode="local" if demo_only else ("external" if managed else "disabled"),
            assurance=(
                "demo-only"
                if demo_only
                else (assurance("evaluation-gate") if managed else "unavailable")
            ),
            provider="local deterministic scorer" if demo_only else "Hrz4",
            reason=(
                "smoke evaluation only; not the promotion authority"
                if demo_only
                else ("" if managed else "production replacement is not configured")
            ),
            required_for_production=True,
        ),
        *[
            CapabilityModel(
                name=name,
                available=managed and configured,
                mode="external"
                if settings.profile == "platform"
                else ("managed" if managed else "disabled"),
                assurance=assurance(name) if managed and configured else "unavailable",
                provider=provider,
                reason=(
                    "managed service intentionally absent from the laptop profile"
                    if demo_only
                    else (
                        ""
                        if managed and configured and refs[name]
                        else "service endpoint or capability-specific attestation is not configured"
                    )
                ),
                required_for_production=True,
            )
            for name, provider, configured in (
                (
                    "immutable-audit",
                    "Hrz5 / Cloud Logging WORM",
                    optional_setting("HRZ_OBSERVABILITY_URL") is not None,
                ),
                (
                    "observability",
                    "Hrz5 / OpenTelemetry",
                    optional_setting("OTEL_EXPORTER_OTLP_ENDPOINT") is not None,
                ),
                (
                    "model-armor",
                    "Hrz1 / Model Armor",
                    bool(settings.model_armor.template_id.strip()),
                ),
            )
        ],
        CapabilityModel(
            name="embeddable-ui",
            available=core_available,
            mode=settings.channel_mode,
            assurance="demo-only" if demo_only else assurance("embeddable-ui"),
            provider="portable Next.js micro-frontend",
        ),
    ]
    production_ready = not demo_only and all(
        item.available and item.assurance == "attested"
        for item in items
        if item.required_for_production
    )
    return CapabilityManifestModel(
        service="cdd-sow-research",
        profile=settings.profile,
        region=settings.region,
        capabilities=items,
        demo_only=demo_only,
        production_ready=production_ready,
    )


@api_router.get("/v1/personas", tags=["ops"])
def personas() -> list[dict[str, str]]:
    """List seeded dev personas for the local persona picker (empty outside local profile).

    Local mode runs with no IdP; the UI uses this to let a demo/test pick an identity
    (and thus exercise per-user authorization) via the ``X-Dev-Persona`` header. Secure
    profiles resolve identity from the IAP assertion, so this returns an empty list.
    """
    container = deps.get_container()
    # A relaxation (it publishes unauthenticated identities), so it reads the exposure mode:
    # a run that chose no profile lists nothing rather than constructing the persona adapter,
    # which refuses under exactly that condition.
    if container.settings.exposure_identity_mode != "local-persona":
        return []
    identity = container.identity
    lister = getattr(identity, "personas", None)
    if lister is None:
        return []
    return [dict(p) for p in lister()]


@api_router.get(
    "/.well-known/agent-card.json",
    response_model=AgentCardModel,
    tags=["governance"],
)
def agent_card() -> AgentCardModel:
    """Publish this agent's A2A AgentCard for discovery (A3 Registry / interop)."""
    from ..agent.agent_card import build_agent_card

    return AgentCardModel.from_domain(build_agent_card(deps.get_settings()))


app = create_app()


def main() -> None:
    """Run the API locally with uvicorn (Cloud Run / Agent Runtime use this app object).

    Fail-closed binding: the ``local`` profile serves seeded no-auth personas, so it
    binds loopback unless the operator explicitly opts into exposure with
    ``CDD_ALLOW_INSECURE_DEMO=1`` (or overrides ``CDD_API_HOST``). Secure profiles keep
    the container-friendly ``0.0.0.0`` default; ingress is fronted by the platform.
    """
    import uvicorn

    # Fail-closed bind guard, sourced from hex-service-kit: refuses to expose the no-auth local
    # profile off loopback without CDD_ALLOW_INSECURE_DEMO=1, and refuses a CDD_API_HOST that is
    # present but empty rather than letting it inherit the profile default, which on a secure
    # profile would mean binding every interface on a value nobody chose. Both refusals are
    # boot-time operator errors, so both become a SystemExit with the guard's own message
    # instead of an uncaught traceback.
    settings = Settings.load()
    try:
        host = resolve_bind_host(
            # The same value the request-time guard on the app object was built with, so the
            # start-up bind and the serving path cannot disagree.
            _bind_profile(settings),
            host_env="CDD_API_HOST",
            insecure_demo_env="CDD_ALLOW_INSECURE_DEMO",
        )
    except (InsecureBindError, ConfiguredEmptyError) as exc:
        raise SystemExit(str(exc)) from exc
    uvicorn.run(
        "cdd_sow_research.api.app:app",
        host=host,
        port=int(setting_or_default("PORT", "8090")),
        reload=boolean_setting("CDD_API_RELOAD"),
    )


if __name__ == "__main__":
    main()
