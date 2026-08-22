"""Local corporate-registry adapter (CorporateRegistryPort) — deterministic UBO resolver.

The ``local`` profile's stand-in for the grounded registry lookup: a deterministic,
in-process resolver that returns a clearly-fictional beneficial-ownership picture for an
entity. SDK-free and unconditional (there is no emulator for the registry). It never
flags a PEP on its own (a hard signal must come from real evidence), keeping the offline
dossier conservative.
"""

from __future__ import annotations

from ...config import Settings
from ...domain.models import (
    BeneficialOwner,
    Citation,
    OwnershipNode,
    OwnershipSummary,
    SourceType,
)


class LocalRegistryLookupAdapter:
    """Resolve a deterministic, fictional corporate-ownership / UBO picture offline."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def lookup(self, entity_name: str, jurisdiction: str) -> OwnershipSummary:
        citation = Citation(
            source_id="doc-registry",
            source_type=SourceType.REGISTRY,
            title="Corporate Registry Extract (FICTIONAL)",
            url="https://example.test/doc-registry",
            page=2,
            snippet="Beneficial ownership recorded in the registry extract.",
            score=0.9,
        )
        owners = (
            BeneficialOwner(
                name=f"Beneficial owner of {entity_name} (FICTIONAL)",
                pct=75.0,
                country=jurisdiction or "SG",
                is_pep=False,
                citations=(citation,),
            ),
        )
        return OwnershipSummary(
            root_entity=entity_name,
            owners=owners,
            tree=OwnershipNode(
                entity_name=entity_name,
                pct=100.0,
                children=(
                    OwnershipNode(
                        entity_name=f"Beneficial owner of {entity_name} (FICTIONAL)", pct=75.0
                    ),
                ),
            ),
            citations=(citation,),
        )
