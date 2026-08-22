"""FastAPI dependency wiring for the B1 CDD + Source-of-Wealth Agent.

This module builds a single, process-wide :class:`~cdd_sow_research.config.Container` (the
ports-and-adapters registry) and assembles the orchestration services from the
Container's port instances. The Container is created lazily on first access so importing
this module (and therefore the FastAPI app) never touches Google Cloud: a unit test or
the on-prem profile can import the API with no GCP SDK installed.

Each ``get_*`` factory is a FastAPI ``Depends`` provider. Services take *explicit port
instances* in their constructors (SPEC §5), so the wiring here is the single place that
knows which ports each service needs.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from functools import lru_cache

from ..config import Container, Settings, build_container
from ..domain.review_policy import CddReviewPolicy
from ..domain.services import (
    AdverseMediaService,
    CddService,
    OwnershipService,
    PerpetualKycService,
    SourceOfWealthService,
    SowCaseService,
    UboGraphService,
)

_request_container: ContextVar[Container | None] = ContextVar(
    "cdd_sow_agent_request_container",
    default=None,
)


@lru_cache(maxsize=1)
def _default_container() -> Container:
    return build_container(Settings.load())


def get_container() -> Container:
    """Return the process-wide Container, building it on first use."""
    return _request_container.get() or _default_container()


# Preserve the existing test/operational cache-reset surface.
get_container.cache_clear = _default_container.cache_clear  # type: ignore[attr-defined]


def bind_request_container(container: Container) -> Token[Container | None]:
    """Bind one app factory's validated container for this request context."""
    return _request_container.set(container)


def reset_request_container(token: Token[Container | None]) -> None:
    _request_container.reset(token)


def get_settings() -> Settings:
    """Convenience accessor for the active settings (region, profile, models...)."""
    return get_container().settings


# --------------------------------------------------------------------------- #
# Service factories — assemble each service from the Container's ports.
# --------------------------------------------------------------------------- #


def get_cdd_service() -> CddService:
    """CddService(extraction, knowledge_base, adverse_media, registry, compliance, llm,
    guardrail, redaction, tracer, audit)."""
    return build_cdd_service(get_container())


def build_sow_case_service(container: Container) -> SowCaseService:
    """SowCaseService(case_store, gap engine from policy, audit) — the longitudinal flow.

    The 2nd positional to ``from_policy`` is the bank-owned ``RiskPolicy`` (its ``gap``
    section parameterises the deterministic reconciliation/gap engine).
    """
    return SowCaseService.from_policy(
        container.case_store, container.settings.policy, audit=container.audit
    )


def get_sow_case_service() -> SowCaseService:
    """FastAPI provider for the SoW-case service (managed case store bound by profile)."""
    return build_sow_case_service(get_container())


def build_perpetual_kyc_service(container: Container) -> PerpetualKycService:
    """Assemble the perpetual-KYC orchestrator from an explicit Container.

    Every number the module uses (signal uplifts, the score ceiling, the priority
    thresholds and the SLA days) comes from the bank-owned ``policy:`` section, so a
    compliance function retunes perpetual KYC through configuration, never a code change.
    """
    return PerpetualKycService.from_policy(
        container.settings.policy,
        sanctions=container.sanctions,
        adverse_media=container.adverse_media,
        registry=container.registry,
        store=container.monitoring_store,
        review_router=container.review_router,
        audit=container.audit,
        tracer=container.tracer,
        redaction=container.redaction,
        llm=container.llm,
    )


def get_perpetual_kyc_service() -> PerpetualKycService:
    """FastAPI provider for the perpetual-KYC orchestrator (profile-bound ports)."""
    return build_perpetual_kyc_service(get_container())


def build_ubo_graph_service(container: Container) -> UboGraphService:
    """Assemble the UBO-graph orchestrator from an explicit Container.

    Every threshold the module applies (the beneficial-ownership percentage, the
    control ladder's rungs, the depth limits and the indicator weights) comes from the
    bank-owned ``policy:`` section, so a compliance function moves the 25% threshold to
    10% through configuration, never a code change. No store port: a resolution is a
    pure function of the registry layers plus policy, and is therefore recomputable.
    """
    return UboGraphService.from_policy(
        container.settings.policy,
        ownership_graph=container.ownership_graph,
        review_router=container.review_router,
        audit=container.audit,
        tracer=container.tracer,
        redaction=container.redaction,
        llm=container.llm,
    )


def get_ubo_graph_service() -> UboGraphService:
    """FastAPI provider for the UBO-graph orchestrator (profile-bound ports)."""
    return build_ubo_graph_service(get_container())


def build_cdd_service(container: Container) -> CddService:
    """Assemble a :class:`CddService` from an explicit Container.

    The maker-checker escalation thresholds come from the bank-owned ``policy:``
    section of settings (P-06 stays code-enforced; the *thresholds* are config).
    """
    return CddService(
        extraction=container.extraction,
        knowledge_base=container.knowledge_base,
        adverse_media=container.adverse_media,
        registry=container.registry,
        compliance=container.compliance,
        llm=container.llm,
        guardrail=container.guardrail,
        redaction=container.redaction,
        tracer=container.tracer,
        audit=container.audit,
        review_policy=CddReviewPolicy.from_policy(container.settings.policy.escalation),
        review_router=container.review_router,
        # Custody of the uploaded documents a case names: the pipeline reads their bytes
        # back through the same fail-closed ACL that governs retrieval.
        document_store=container.document_store,
        # Watchlist snapshot for deterministic sanctions/PEP screening: every dossier
        # carries a reproducible screening result (or an honest "not screened").
        sanctions=container.sanctions,
    )


def build_sow_service(container: Container) -> SourceOfWealthService:
    """Assemble a :class:`SourceOfWealthService` from an explicit Container."""
    return SourceOfWealthService(llm=container.llm, tracer=container.tracer)


def build_adverse_media_service(container: Container) -> AdverseMediaService:
    """Assemble an :class:`AdverseMediaService` from an explicit Container."""
    return AdverseMediaService(adverse_media=container.adverse_media, tracer=container.tracer)


def build_ownership_service(container: Container) -> OwnershipService:
    """Assemble an :class:`OwnershipService` from an explicit Container."""
    return OwnershipService(registry=container.registry, tracer=container.tracer)


def create_app():  # -> fastapi.FastAPI
    """Application factory used by uvicorn (``--factory``) and the CLI ``serve`` command."""
    from .app import create_app as build_app

    return build_app()
