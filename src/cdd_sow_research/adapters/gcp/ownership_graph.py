"""Grounded ownership-graph adapter (OwnershipGraphPort) — ONE cited registry hop.

The managed sibling of ``gcp/registry_lookup.py``. Where that adapter asks for a flat UBO
summary in one shot, this one asks a much narrower question: **who is recorded directly
against this one entity**, and returns exactly that, cited. The engine
(``domain/ubo_graph.py``) then decides which entity to ask about next.

That narrowness is the point. A prompt that asks a model to "resolve the whole ownership
structure" invites it to invent the layers it cannot find, and the invented layers would
be indistinguishable from the found ones in the answer. Asking one hop at a time means the
depth limit, the visited set, the percentage arithmetic and the truncation flag all live
in pure code an auditor can recompute, and the model's only job is to read one filing.

A registry API integration plugs in behind this same port unchanged: the port's contract
is a hop, not a prompt.

All Google GenAI SDK imports are lazy so the on-prem / test profile imports this module
without ``google-genai`` installed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ...config import Settings
from ...domain._grounded import parse_json_object
from ...domain.models import (
    Citation,
    OwnershipEdge,
    OwnershipEdgeKind,
    OwnershipGraphNode,
    OwnershipNodeKind,
    RegistryHop,
    SourceType,
)
from ...domain.ubo_graph import ownership_node_id

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from google import genai

_PROMPT = (
    "From public corporate-registry sources, report ONLY what is recorded DIRECTLY "
    "against the entity below: its immediate shareholders, the holders of its voting "
    "rights, its directors, any declared nominee arrangement and any party with "
    "contractual control. Do NOT follow the chain upwards and do NOT infer an ultimate "
    "beneficial owner: report one layer only.\n\n"
    "For the entity itself report: jurisdiction, registered address, incorporation date "
    "and current filing status. For each party recorded against it report: name, whether "
    "it is a natural_person / entity / trust / nominee / state / listed company, its "
    "jurisdiction, the relationship (shareholding | voting | directorship | "
    "nominee_arrangement | contractual) and the percentage where one is recorded.\n\n"
    "If the registry has no record for the entity, return "
    '{{"resolved": false, "owners": []}} rather than guessing.\n\n'
    'Return strictly JSON: {{"entity": {{...}}, "owners": [ ... ], "resolved": true}}.\n\n'
    "Entity: {entity_name}\nJurisdiction: {jurisdiction}"
)

#: The relationship vocabulary the prompt asks for, mapped onto the domain enum. Anything
#: the model returns that is not in this table is read as a plain shareholding, because a
#: relationship we cannot classify is still a recorded connection worth showing a human.
_EDGE_KINDS: dict[str, OwnershipEdgeKind] = {k.value: k for k in OwnershipEdgeKind}
_NODE_KINDS: dict[str, OwnershipNodeKind] = {k.value: k for k in OwnershipNodeKind}

#: Percentage spellings a grounded answer actually uses (see registry_lookup._PCT_KEYS:
#: the same tolerance, for the same reason).
_PCT_KEYS = ("pct", "percentage", "ownership_percentage", "share_percentage")


class GroundedOwnershipGraphAdapter:
    """One cited registry hop via grounded search; traversal stays in the engine."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._models = settings.models
        self._client: Any | None = None

    def _get_client(self) -> genai.Client:
        if self._client is None:
            from google import genai

            self._client = genai.Client(
                vertexai=True,
                project=self._settings.project_id,
                location=self._settings.region,
            )
        return self._client

    def hop(self, entity_name: str, jurisdiction: str) -> RegistryHop:
        """Return the parties recorded directly against ``entity_name``."""
        from google.genai import types

        client = self._get_client()
        response = client.models.generate_content(
            model=self._models.reasoning,
            contents=types.Content(
                role="user",
                parts=[
                    types.Part.from_text(
                        text=_PROMPT.format(
                            entity_name=entity_name, jurisdiction=jurisdiction or "unknown"
                        )
                    )
                ],
            ),
            config=types.GenerateContentConfig(
                temperature=0.0,
                tools=[types.Tool(google_search=types.GoogleSearch())],
            ),
        )
        return self._parse(entity_name, jurisdiction, getattr(response, "text", "") or "")

    @classmethod
    def _parse(cls, entity_name: str, jurisdiction: str, text: str) -> RegistryHop:
        """Tolerant read of a grounded answer into one hop (see registry_lookup._parse).

        An unparseable answer becomes an UNRESOLVED hop, never an empty resolved one: the
        engine flags an unresolved layer as an opacity indicator, whereas a resolved-but-
        empty hop would read as "this entity transparently has no owners", which is the
        exact misreading a UBO tool must not produce.
        """
        node_id = ownership_node_id(entity_name, jurisdiction)
        citation = Citation(
            source_id=f"registry-hop:{node_id}",
            source_type=SourceType.REGISTRY,
            title=f"Corporate registry extract for {entity_name}",
            snippet=f"Parties recorded directly against {entity_name}.",
        )
        data = parse_json_object(text)
        candidate = data.get("entity")
        raw_entity: dict[str, Any] = candidate if isinstance(candidate, dict) else {}
        entity = OwnershipGraphNode(
            id=node_id,
            name=entity_name,
            kind=OwnershipNodeKind.ENTITY,
            jurisdiction=str(raw_entity.get("jurisdiction") or jurisdiction or "").strip(),
            registered_address=str(raw_entity.get("registered_address") or "").strip(),
            incorporation_date=str(raw_entity.get("incorporation_date") or "").strip(),
            status=str(raw_entity.get("status") or "").strip(),
            citations=(citation,),
        )
        if not data or data.get("resolved") is False:
            return RegistryHop(entity=entity, resolved=False, citations=(citation,))

        owners: list[OwnershipGraphNode] = []
        edges: list[OwnershipEdge] = []
        seen: set[str] = set()
        for raw in data.get("owners") or []:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or "").strip()
            if not name:
                continue
            owner_jurisdiction = str(raw.get("jurisdiction") or "").strip()
            owner_id = ownership_node_id(name, owner_jurisdiction)
            owner_citation = Citation(
                source_id=f"registry-hop:{node_id}:{owner_id}",
                source_type=SourceType.REGISTRY,
                title=f"Corporate registry extract for {entity_name}",
                snippet=f"{name} is recorded against {entity_name}.",
            )
            if owner_id not in seen:
                seen.add(owner_id)
                owners.append(
                    OwnershipGraphNode(
                        id=owner_id,
                        name=name,
                        kind=_NODE_KINDS.get(
                            str(raw.get("kind") or raw.get("type") or "").strip().lower(),
                            OwnershipNodeKind.UNKNOWN,
                        ),
                        jurisdiction=owner_jurisdiction,
                        registered_address=str(raw.get("registered_address") or "").strip(),
                        incorporation_date=str(raw.get("incorporation_date") or "").strip(),
                        status=str(raw.get("status") or "").strip(),
                        is_pep=bool(raw.get("is_pep") or raw.get("pep")),
                        citations=(owner_citation,),
                    )
                )
            edges.append(
                OwnershipEdge(
                    source_id=owner_id,
                    target_id=node_id,
                    kind=_EDGE_KINDS.get(
                        str(raw.get("relationship") or raw.get("kind") or "").strip().lower(),
                        OwnershipEdgeKind.SHAREHOLDING,
                    ),
                    pct=cls._pct_of(raw),
                    as_of=str(raw.get("as_of") or "").strip(),
                    citations=(owner_citation,),
                )
            )
        return RegistryHop(
            entity=entity,
            owners=tuple(owners),
            edges=tuple(edges),
            citations=(citation,),
            resolved=True,
        )

    @staticmethod
    def _pct_of(raw: dict[str, Any]) -> float:
        for key in _PCT_KEYS:
            if raw.get(key) is not None:
                try:
                    return float(raw[key])
                except (TypeError, ValueError):
                    return 0.0
        return 0.0
