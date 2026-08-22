"""OwnershipService — corporate-ownership / UBO resolution (SPEC §5).

A thin domain service over the :class:`CorporateRegistryPort`: for an entity subject it
asks the registry for the beneficial-ownership picture and returns the
:class:`OwnershipSummary`. For an individual subject there is no corporate tree to
resolve, so it returns an empty summary rooted at the subject's name (the source-of
wealth and adverse-media artifacts still apply).

Pure domain code: talks only to ports and models, no Google Cloud / ADK imports.
"""

from __future__ import annotations

from typing import Any

from .models import OwnershipSummary, Subject, SubjectType


class OwnershipService:
    """Resolve the corporate-ownership / UBO picture. Signature fixed by SPEC §5."""

    def __init__(self, registry: Any, tracer: Any) -> None:
        self._registry = registry
        self._tracer = tracer

    def resolve(self, subject: Subject, actor: str) -> OwnershipSummary:
        """Resolve the ownership summary for ``subject`` (entities only)."""
        if subject.type is not SubjectType.ENTITY:
            return OwnershipSummary(root_entity=subject.name)
        try:
            summary = self._registry.lookup(subject.name, subject.jurisdiction)
        except Exception:  # noqa: BLE001 - registry lookup is best-effort, never fatal
            return OwnershipSummary(root_entity=subject.name)
        return summary or OwnershipSummary(root_entity=subject.name)
