"""Research ports — adverse-media scanning and corporate-registry lookup.

Primary GCP adapters: the Gemini API ``google_search`` grounding tool isolated in its
own sub-agent (one built-in tool per agent) for adverse-media, and a grounded
registry lookup for corporate ownership / UBO. Both keep web egress in the grounding
sub-agent so the rest of the agent stays inside the tenancy.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..domain.models import AdverseMediaScreening, OwnershipSummary


@runtime_checkable
class AdverseMediaPort(Protocol):
    def search(self, subject_name: str, max_results: int = 10) -> AdverseMediaScreening | None:
        """Return the adverse-media screen for ``subject_name``, or ``None`` if none ran.

        An adapter with no reachable backend returns ``None``. It must never return an empty
        screening to mean "could not search": an empty screening is what a search that ran
        and found nothing looks like, and the dossier renders the two differently.
        """
        ...


@runtime_checkable
class CorporateRegistryPort(Protocol):
    def lookup(self, entity_name: str, jurisdiction: str) -> OwnershipSummary:
        """Resolve the corporate-ownership / UBO picture for an entity."""
        ...
