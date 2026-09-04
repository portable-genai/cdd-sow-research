"""UboGraphService — the UBO-graph orchestrator (SPEC §11).

Resolves one subject's cross-jurisdiction beneficial-ownership structure inside the
existing CDD lifecycle, in the same five moves the perpetual-KYC orchestrator makes:

1. read the registry ONE HOP AT A TIME through the ``OwnershipGraphPort``, best-effort:
   a hop that fails becomes an UNRESOLVED layer the engine flags, never an exception that
   loses the rest of the structure;
2. compute the whole outcome in PURE code with
   :class:`~cdd_sow_research.domain.ubo_graph.UboGraphEngine` (the walk, the per-path
   arithmetic, the control ladder, the nominee/shell indicators and the opacity score);
3. optionally ask the LLM to NARRATE that finished resolution, after redaction, with
   every number already fixed: the narrative is prose about a structure the model did not
   resolve, schema-validated and discarded on failure;
4. route the resolution to human-review-console for maker-checker disposition (rule R8); and
5. write the already-redacted WORM audit record.

**No store port.** A resolution is a pure function of the registry layers plus policy, so
it is recomputable rather than stateful: nothing here is a delta against a remembered
baseline the way perpetual KYC is, and persisting it would create a second, staler answer
to a question the engine can answer exactly. The durable record of a resolution is the
human-review-console review item plus the WORM audit event, both of which already carry it.

Every consequential value is arithmetic the engine performed and an auditor can recompute.
The service never blocks, exits or downgrades a relationship, and never concludes that a
structure conceals anything: it produces a cited, explainable picture and routes it.

Pure domain code: talks only to ports and models, no Google Cloud / ADK imports.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from . import _grounded as g
from .entitlements import case_tags
from .models import (
    AuditEvent,
    Decision,
    OwnershipGraph,
    OwnershipSummary,
    RegistryHop,
    Subject,
    SubjectType,
    ThinkingLevel,
    UboResolution,
)
from .policy import RiskPolicy
from .prompts import NARRATIVE_SCHEMA, UBO_GRAPH_NARRATIVE_PROMPT
from .ubo_graph import UboGraphEngine, ownership_node_id, to_ownership_summary

_LOG = logging.getLogger(__name__)

#: Hard cap on the narrative the model may return. The narrative is decoration on a
#: resolution the code already computed, so an over-long answer is truncated, never
#: trusted.
_MAX_NARRATIVE_CHARS = 1200


class UboGraphService:
    """Orchestrate one UBO-graph resolution. Signature fixed by SPEC §11."""

    def __init__(
        self,
        *,
        ownership_graph: Any,
        review_router: Any,
        audit: Any,
        tracer: Any,
        redaction: Any | None = None,
        llm: Any | None = None,
        engine: UboGraphEngine | None = None,
    ) -> None:
        self._ownership_graph = ownership_graph
        self._review_router = review_router
        self._audit = audit
        self._tracer = tracer
        self._redaction = redaction
        self._llm = llm
        self._engine = engine or UboGraphEngine()

    @classmethod
    def from_policy(
        cls,
        policy: RiskPolicy,
        *,
        ownership_graph: Any,
        review_router: Any,
        audit: Any,
        tracer: Any,
        redaction: Any | None = None,
        llm: Any | None = None,
    ) -> UboGraphService:
        """Build the service with every number taken from bank-owned policy (B4)."""
        return cls(
            ownership_graph=ownership_graph,
            review_router=review_router,
            audit=audit,
            tracer=tracer,
            redaction=redaction,
            llm=llm,
            engine=UboGraphEngine.from_policy(policy.ubo_graph, policy.country_risk),
        )

    # ------------------------------------------------------------------ #
    def resolve(
        self,
        subject: Subject,
        *,
        actor: str,
        as_of: date,
        narrate: bool = True,
    ) -> UboResolution:
        """Resolve, narrate, route and audit one subject's ownership structure.

        The record's ACL is stamped from the SUBJECT (whose tenant the API has already
        taken from the verified principal), so a resolution can never be routed or
        audited under another tenant's tags.
        """
        with self._tracer.span("ubo_graph.resolve", action="ubo_graph", actor=actor):
            resolution = self._engine.resolve(
                subject=subject,
                as_of=as_of,
                fetch=self._fetch,
                acl=case_tags(subject.id, subject.tenant),
            )
            if narrate:
                resolution = self._narrate(resolution)

            routed = self._route(resolution, maker=actor)
            resolution = _with_routed_flag(resolution, routed)
            self._write_audit(resolution, actor=actor, routed=routed)
            return resolution

    def graph(self, subject: Subject, *, actor: str, as_of: date) -> OwnershipGraph:
        """The walked structure alone: layers, edges and citations, no verdict.

        Evidence, not a decision. It carries no finding, no control basis, no indicator
        and no opacity score, so it engages neither the maker-checker rule nor rule R8,
        which is what lets it be a side-effect-free read (and a cheap one for a consumer
        that only wants the chain).
        """
        with self._tracer.span("ubo_graph.graph", action="ubo_graph_read", actor=actor):
            return self._engine.traverse(subject=subject, as_of=as_of, fetch=self._fetch)

    @staticmethod
    def summary(resolution: UboResolution) -> OwnershipSummary:
        """The flat one-hop :class:`OwnershipSummary` the dossier already consumes."""
        return to_ownership_summary(resolution)

    # ------------------------------------------------------------------ #
    # Observation: one hop at a time, best-effort
    # ------------------------------------------------------------------ #
    def _fetch(self, entity_name: str, jurisdiction: str) -> RegistryHop:
        """One registry hop; a failed hop degrades to an UNRESOLVED layer, never an error.

        A dead or rate-limited registry on layer four must not lose layers one to three.
        Returning an unresolved hop is honest about the gap: the engine raises an
        ``unresolved_layer`` indicator, the opacity score rises, and the reviewer is told
        the percentages below it are a floor.
        """
        try:
            hop = self._ownership_graph.hop(entity_name, jurisdiction)
        except Exception:  # noqa: BLE001 - one dead layer must not lose the structure
            _LOG.warning("ownership-graph hop unavailable for %s", entity_name)
            return _unresolved(entity_name, jurisdiction)
        if not isinstance(hop, RegistryHop):  # pragma: no cover - defensive
            _LOG.warning("ownership-graph adapter returned a non-hop for %s", entity_name)
            return _unresolved(entity_name, jurisdiction)
        return hop

    # ------------------------------------------------------------------ #
    # Narration: prose only, after redaction, over a structure already resolved
    # ------------------------------------------------------------------ #
    def _narrate(self, resolution: UboResolution) -> UboResolution:
        if self._llm is None:
            return resolution
        facts = self._facts(resolution)
        if self._redaction is not None:
            try:
                facts = self._redaction.redact(facts).text
            except Exception:  # noqa: BLE001 - never send un-redacted text to a model
                _LOG.warning("UBO-graph redaction failed; narration skipped")
                return resolution
        try:
            response = self._llm.generate(
                g.build_llm_request(
                    UBO_GRAPH_NARRATIVE_PROMPT,
                    facts,
                    None,
                    NARRATIVE_SCHEMA,
                    thinking=ThinkingLevel.LOW,
                    max_output_tokens=512,
                )
            )
            # Schema-validated: anything that is not a non-empty ``narrative`` string is
            # discarded rather than rendered, and no number is ever read back.
            payload = g.parse_structured(response)
        except Exception:  # noqa: BLE001 - narration is decoration, never load-bearing
            _LOG.warning("UBO-graph narration unavailable for %s", resolution.subject_id)
            return resolution
        g.maybe_record_usage(self._tracer, response)
        text = payload.get("narrative")
        if not isinstance(text, str) or not text.strip():
            return resolution
        from dataclasses import replace

        return replace(resolution, narrative=text.strip()[:_MAX_NARRATIVE_CHARS])

    @staticmethod
    def _facts(resolution: UboResolution) -> str:
        """The already-computed facts the narrator may restate, and nothing else."""
        graph = resolution.graph
        lines = [
            f"subject_id: {resolution.subject_id}",
            f"as_of: {resolution.as_of}",
            f"layers: {graph.depth if graph is not None else 0}",
            f"parties: {len(graph.nodes) if graph is not None else 0}",
            f"jurisdictions: {', '.join(graph.jurisdictions) if graph is not None else ''}",
            f"structure_truncated: {str(bool(graph is not None and graph.truncated)).lower()}",
            f"ownership_threshold_pct: {resolution.ownership_threshold_pct:.2f}",
            f"control_basis: {resolution.control_basis.value}",
            f"opacity_score: {resolution.opacity_score:.4f}",
        ]
        for finding in resolution.beneficial_owners:
            lines.append(
                f"beneficial_owner: {finding.name} at "
                f"{finding.effective_pct:.4f}% effective ownership"
            )
        for finding in resolution.controllers:
            lines.append(f"controller: {finding.name} ({finding.control_reason})")
        for flag in resolution.flags:
            lines.append(f"indicator: {flag.kind.value} - {flag.summary}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    def _route(self, resolution: UboResolution, *, maker: str) -> bool:
        try:
            self._review_router.route_ownership(resolution, maker=maker)
        except Exception:  # noqa: BLE001 - a console outage must not lose the resolution
            _LOG.error(
                "UBO-graph review routing failed for %s; the resolution still requires "
                "human review",
                resolution.subject_id,
            )
            return False
        return True

    def _write_audit(self, resolution: UboResolution, *, actor: str, routed: bool) -> None:
        graph = resolution.graph
        try:
            self._audit.record(
                AuditEvent(
                    action="ubo_graph.resolve",
                    actor=actor,
                    decision=Decision.ESCALATED,
                    redacted_prompt=f"UBO-graph resolution for {resolution.subject_id}",
                    redacted_response=resolution.rationale,
                    citations=resolution.citations,
                    metadata={
                        "subject_id": resolution.subject_id,
                        "as_of": resolution.as_of,
                        "control_basis": resolution.control_basis.value,
                        "beneficial_owners": str(len(resolution.beneficial_owners)),
                        "opacity_score": f"{resolution.opacity_score:.4f}",
                        "depth": str(graph.depth if graph is not None else 0),
                        "truncated": str(bool(graph is not None and graph.truncated)).lower(),
                        "routed_to_hrz7": str(routed).lower(),
                        "requires_human_review": "true",
                    },
                )
            )
        except Exception:  # noqa: BLE001 - audit failure must not lose the resolution
            _LOG.exception("UBO-graph audit write failed for %s", resolution.subject_id)


def _unresolved(entity_name: str, jurisdiction: str) -> RegistryHop:
    """The honest answer when a layer cannot be read: present, but not resolved."""
    from .models import OwnershipGraphNode, OwnershipNodeKind

    return RegistryHop(
        entity=OwnershipGraphNode(
            id=ownership_node_id(entity_name, jurisdiction),
            name=entity_name,
            kind=OwnershipNodeKind.UNKNOWN,
            jurisdiction=jurisdiction,
        ),
        resolved=False,
    )


def _with_routed_flag(resolution: UboResolution, routed: bool) -> UboResolution:
    from dataclasses import replace

    return replace(resolution, routed_to_hrz7=routed)


def subject_is_resolvable(subject: Subject) -> bool:
    """Only a corporate entity has an ownership structure to walk (P-06 clarity)."""
    return subject.type is SubjectType.ENTITY
