"""Grounded corporate-registry adapter (CorporateRegistryPort).

Resolves the corporate-ownership / UBO picture for an entity via Gemini grounded search
on the Gemini Enterprise Agent Platform (a registry API integration plugs in the same
way behind this port). The model is prompted to return the beneficial-ownership tree and
the natural-person owners with their percentages and PEP flags, mapped onto a domain
:class:`OwnershipSummary` with ``REGISTRY`` citations.

All Google GenAI SDK imports are lazy so the on-prem / test profile imports this module
without ``google-genai`` installed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ...config import Settings
from ...domain._grounded import parse_json_object
from ...domain.models import (
    BeneficialOwner,
    Citation,
    OwnershipNode,
    OwnershipSummary,
    SourceType,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from google import genai

_PROMPT = (
    "Look up the corporate ownership and ultimate beneficial owners for the entity "
    "below from public corporate-registry sources. Return the beneficial owners (name, "
    "ownership percentage, country, whether they are a politically exposed person) and "
    "an ownership tree. If the registry is opaque, return what is known and flag the "
    "gaps.\n\n"
    'Return strictly JSON: {{"owners": [ ... ], "tree": {{...}}}}.\n\n'
    "Entity: {entity_name}\nJurisdiction: {jurisdiction}"
)


class GroundedRegistryAdapter:
    """Corporate-registry / UBO resolution via grounded search."""

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
                # The MODEL location, not the compute region: see models.location in
                # config/settings.yaml. us-central1 serves no Gemini 3.
                location=self._settings.models.location,
            )
        return self._client

    def lookup(self, entity_name: str, jurisdiction: str) -> OwnershipSummary:
        """Resolve the ownership / UBO picture for ``entity_name``."""
        from google.genai import types

        client = self._get_client()
        response = client.models.generate_content(
            model=self._models.reasoning,
            # A single Content, not a one-element list: the SDK accepts both, and the
            # bare form types cleanly (list[Content] is invariant against the union).
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
        return self._parse(entity_name, getattr(response, "text", "") or "")

    @staticmethod
    def _parse(entity_name: str, text: str) -> OwnershipSummary:
        # Tolerant read: see GeminiAdverseMediaAdapter._parse. A grounded answer is
        # routinely fenced, and parsing it strictly reports an entity as having no
        # known beneficial owners, which reads as a transparent structure rather than
        # as the parse failure it actually is.
        data = parse_json_object(text)
        if not data:
            return OwnershipSummary(root_entity=entity_name)

        citation = Citation(
            source_id=f"registry:{entity_name}",
            source_type=SourceType.REGISTRY,
            title=f"Corporate registry extract for {entity_name}",
        )
        owners: list[BeneficialOwner] = []
        for raw in data.get("owners") or []:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or "").strip()
            if not name:
                continue
            owners.append(
                BeneficialOwner(
                    name=name,
                    pct=GroundedRegistryAdapter._pct_of(raw),
                    country=str(raw.get("country") or "").strip(),
                    is_pep=bool(raw.get("is_pep") or raw.get("pep")),
                    citations=(citation,),
                )
            )
        tree = GroundedRegistryAdapter._tree(data.get("tree"), entity_name)
        return OwnershipSummary(
            root_entity=entity_name,
            owners=tuple(owners),
            tree=tree,
            citations=(citation,),
        )

    @staticmethod
    def _tree(raw: Any, root_name: str) -> OwnershipNode:
        if not isinstance(raw, dict):
            return OwnershipNode(entity_name=root_name)
        name = str(raw.get("entity_name") or raw.get("name") or root_name)
        children = tuple(
            GroundedRegistryAdapter._tree(child, name)
            for child in (raw.get("children") or [])
            if isinstance(child, dict)
        )
        return OwnershipNode(
            entity_name=name, pct=GroundedRegistryAdapter._pct_of(raw), children=children
        )

    #: The prompt asks for "ownership percentage" in prose, so the model picks its own
    #: key for it. Read every spelling it actually uses, in owners AND in the ownership
    #: tree: a stake silently read as 0% understates control in a UBO summary, which is
    #: the one number the summary exists to get right.
    _PCT_KEYS = ("pct", "percentage", "ownership_percentage")

    @staticmethod
    def _pct_of(raw: Any) -> float:
        if not isinstance(raw, dict):
            return 0.0
        for key in GroundedRegistryAdapter._PCT_KEYS:
            if raw.get(key) is not None:
                return GroundedRegistryAdapter._pct(raw[key])
        return 0.0

    @staticmethod
    def _pct(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0
