"""ADK FunctionTools that expose the B1 domain services to the agent.

Each tool is a thin, side-effect-honest wrapper: it builds the relevant domain service
from a :class:`~cdd_sow_research.config.Container` (so every port is bound to the adapter
selected by the active profile), invokes the service, and returns a JSON-safe dict via
:func:`~cdd_sow_research.domain.serialization.to_jsonable`.

Design notes
------------
* The domain services own orchestration (redact -> guardrail -> ingest -> retrieve ->
  synthesise -> rate -> compliance check -> guardrail -> audit; SPEC §5). These tools add
  **no** business logic of their own: the model decides *which* artifact to produce, the
  service decides *how*.
* ``google.adk`` is imported lazily inside :func:`build_function_tools` so this module
  imports cleanly under the on-prem/test profile with no ADK installed (SPEC §4). The plain
  Python tool callables are importable and unit-testable without ADK at all.
* Every callable carries a precise type-hinted signature and docstring: ADK derives the
  tool's name, description and JSON parameter schema from them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..config import Container, Settings, build_container

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from google.adk.tools import FunctionTool

_DEFAULT_ACTOR = "cdd-sow-research"


def _container(settings: Settings | None) -> Container:
    return build_container(settings)


def assess_cdd(
    subject_name: str,
    subject_type: str = "entity",
    jurisdiction: str = "",
    actor: str = _DEFAULT_ACTOR,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Build a full cited CDD dossier for a subject.

    Produces a ``CDDCase`` (source-of-wealth narrative, risk rating, adverse-media
    findings, UBO summary) for the named subject. Always flagged for human review.

    Args:
      subject_name: The customer/entity name to assess.
      subject_type: "individual" or "entity".
      jurisdiction: ISO-ish country/region code (e.g. "SG").
      actor: Authenticated identity the request is made for.

    Returns:
      A JSON-safe ``CDDCase`` dict.
    """
    from ..domain.models import CaseInput, Subject, SubjectType
    from ..domain.serialization import to_jsonable
    from ..domain.services import CddService

    c = _container(settings)
    service = CddService(
        extraction=c.extraction,
        knowledge_base=c.knowledge_base,
        adverse_media=c.adverse_media,
        registry=c.registry,
        compliance=c.compliance,
        llm=c.llm,
        guardrail=c.guardrail,
        redaction=c.redaction,
        tracer=c.tracer,
        audit=c.audit,
    )
    subject = Subject(
        id=subject_name.lower().replace(" ", "-"),
        name=subject_name,
        type=SubjectType(subject_type)
        if subject_type in ("individual", "entity")
        else SubjectType.ENTITY,
        jurisdiction=jurisdiction,
    )
    return to_jsonable(service.assess(CaseInput(subject=subject), actor))


def scan_adverse_media(
    subject_name: str,
    actor: str = _DEFAULT_ACTOR,
    settings: Settings | None = None,
) -> dict[str, Any] | None:
    """Scan public-web adverse media for a subject name.

    Args:
      subject_name: The name to scan for negative news.
      actor: Authenticated identity the request is made for.

    Returns:
      A JSON-safe ``AdverseMediaScreening`` dict holding categorised, severity-ranked
      findings with citations, or ``None`` when no screen ran because this profile has no
      reachable adverse-media backend. ``None`` is not a clean result and must not be
      narrated as one; a screening whose ``findings`` are empty is the clean result.
    """
    from ..domain.models import Subject
    from ..domain.serialization import to_jsonable
    from ..domain.services import AdverseMediaService

    c = _container(settings)
    service = AdverseMediaService(adverse_media=c.adverse_media, tracer=c.tracer)
    screening = service.scan(Subject(id="adhoc", name=subject_name), actor)
    return None if screening is None else to_jsonable(screening)


def resolve_ownership(
    entity_name: str,
    jurisdiction: str = "",
    actor: str = _DEFAULT_ACTOR,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Resolve the corporate-ownership / UBO picture for an entity.

    Returns an ``OwnershipSummary`` with beneficial owners (and PEP flags) plus the
    ownership tree.

    Args:
      entity_name: The entity to resolve.
      jurisdiction: ISO-ish country/region code (e.g. "SG").
      actor: Authenticated identity the request is made for.

    Returns:
      A JSON-safe ``OwnershipSummary`` dict.
    """
    from ..domain.models import Subject, SubjectType
    from ..domain.serialization import to_jsonable
    from ..domain.services import OwnershipService

    c = _container(settings)
    service = OwnershipService(registry=c.registry, tracer=c.tracer)
    subject = Subject(
        id="adhoc", name=entity_name, type=SubjectType.ENTITY, jurisdiction=jurisdiction
    )
    return to_jsonable(service.resolve(subject, actor))


def run_perpetual_kyc(
    subject_name: str,
    subject_type: str = "entity",
    jurisdiction: str = "",
    tenant: str = "demo-bank",
    as_of: str = "",
    last_reviewed: str = "",
    actor: str = _DEFAULT_ACTOR,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Run one perpetual-KYC cycle for a subject: detect change, re-score, queue review.

    Compares the current sanctions, adverse-media and corporate-registry picture against
    the stored baseline, re-scores the relationship with deterministic code (the model
    never produces the number), and places an explainable item on the human review queue.
    The outcome always requires human review and is routed to human-review-console; nothing is acted
    on.

    Args:
      subject_name: The customer/entity name to monitor.
      subject_type: "individual" or "entity".
      jurisdiction: ISO-ish country/region code (e.g. "SG").
      tenant: Owning tenant; scopes the monitoring record's ACL.
      as_of: ISO date to evaluate for (empty means today). Makes a run replayable.
      last_reviewed: ISO date of the last completed periodic review.
      actor: Authenticated identity the request is made for.

    Returns:
      A JSON-safe ``PerpetualKycAssessment`` dict (signals, uplifts, queue item).
    """
    from datetime import UTC, date, datetime

    from ..api import deps
    from ..domain.models import Subject, SubjectType
    from ..domain.serialization import to_jsonable

    c = _container(settings)
    service = deps.build_perpetual_kyc_service(c)
    subject = Subject(
        id=subject_name.lower().replace(" ", "-"),
        name=subject_name,
        type=SubjectType(subject_type)
        if subject_type in ("individual", "entity")
        else SubjectType.ENTITY,
        jurisdiction=jurisdiction,
        tenant=tenant,
    )
    when = date.fromisoformat(as_of[:10]) if as_of else datetime.now(UTC).date()
    assessment = service.run(
        subject,
        actor=actor,
        principals=("group:cdd-analyst", f"tenant:{tenant}", f"case:{subject.id}"),
        as_of=when,
        last_reviewed=last_reviewed,
    )
    return to_jsonable(assessment)


def list_perpetual_kyc_queue(
    tenant: str = "demo-bank",
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    """List a tenant's perpetual-KYC review queue, most urgent first.

    Each item explains why the relationship surfaced (the signals that moved, the score
    arithmetic and the citations behind them) and carries its disposition due date.

    Args:
      tenant: The tenant whose queue to list; the listing is scoped to it.

    Returns:
      A JSON-safe list of ``PerpetualKycAssessment`` dicts.
    """
    from ..api import deps
    from ..domain.serialization import to_jsonable

    service = deps.build_perpetual_kyc_service(_container(settings))
    queue = service.queue(("group:cdd-analyst", f"tenant:{tenant}"))
    return to_jsonable(queue)


def resolve_ubo_graph(
    entity_name: str,
    jurisdiction: str = "",
    tenant: str = "demo-bank",
    as_of: str = "",
    actor: str = _DEFAULT_ACTOR,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Resolve an entity's cross-jurisdiction beneficial-ownership graph.

    Walks the corporate structure one cited registry hop at a time and returns the layered
    graph, the natural persons whose EFFECTIVE ownership (the product of the shareholdings
    along each path) reaches the bank's threshold, the basis on which control was
    established, and deterministic nominee/shell indicators with an opacity score. Every
    percentage is computed by pure code; the model produces none of them. The outcome
    always requires human review and is routed to human-review-console; nothing is acted on.

    The returned shape is a FROZEN contract (docs/ubo-graph-contract.md): fields may be
    added, but none is renamed, retyped or removed without an agent-card version bump.

    Args:
      entity_name: The corporate entity whose ownership to resolve.
      jurisdiction: ISO-ish country/region code (e.g. "SG").
      tenant: Owning tenant; scopes the resolution's ACL and the human-review-console review item.
      as_of: ISO date to evaluate for (empty means today). Makes a run replayable.
      actor: Authenticated identity the request is made for.

    Returns:
      A JSON-safe ``UboResolution`` dict (graph, findings with their paths,
      beneficial_owners, control basis, flags, opacity score). ``findings`` carries every
      candidate party including the intermediate holding companies; the owner list is
      ``beneficial_owners``.
    """
    from datetime import UTC, date, datetime

    from ..api import deps
    from ..domain.models import Subject, SubjectType
    from ..domain.serialization import ubo_resolution_jsonable

    c = _container(settings)
    service = deps.build_ubo_graph_service(c)
    subject = Subject(
        id=entity_name.lower().replace(" ", "-"),
        name=entity_name,
        type=SubjectType.ENTITY,
        jurisdiction=jurisdiction,
        tenant=tenant,
    )
    when = date.fromisoformat(as_of[:10]) if as_of else datetime.now(UTC).date()
    # Serialized through the wrapper, not the bare walk: `beneficial_owners` is a computed
    # property and is a frozen key of this skill's contract (docs/ubo-graph-contract.md).
    return ubo_resolution_jsonable(service.resolve(subject, actor=actor, as_of=when))


TOOL_FUNCTIONS = (
    assess_cdd,
    scan_adverse_media,
    resolve_ownership,
    resolve_ubo_graph,
    run_perpetual_kyc,
    list_perpetual_kyc_queue,
)


def build_function_tools() -> list[FunctionTool]:
    """Wrap each domain-service callable as an ADK ``FunctionTool``.

    ADK introspects each function's signature and docstring to derive the tool name,
    description and parameter JSON schema. ``google.adk`` is imported here (lazily) so the
    module is import-safe without ADK installed (SPEC §4).
    """
    from google.adk.tools import FunctionTool

    return [FunctionTool(func=fn) for fn in TOOL_FUNCTIONS]
