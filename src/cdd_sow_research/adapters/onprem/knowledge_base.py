"""On-prem placeholder for ``KnowledgeBaseClientPort`` — the sovereign target.

One of the reversibility (P-02, P-12) migration placeholders: in the platform profile
this port binds to the A2 Enterprise KB HTTP client; switching ``profile`` to ``onprem``
rebinds it here. The adapter constructs cleanly with **no external dependencies** and
structurally satisfies the same Protocol as the managed adapter, so the contract tests
prove interface parity. ``ingest`` / ``search`` deliberately raise rather than silently
dropping a case document or returning empty grounding: an unimplemented governed RAG
store must never quietly de-fang the dossier. Filling these bodies in is the only change
required.
"""

from __future__ import annotations

from ...config import Settings
from ...domain.models import IngestResult, KycDocument, RetrievalQuery, RetrievedPassage

_MESSAGE = (
    "On-prem KnowledgeBaseClientPort adapter is a migration placeholder; implement against "
    "your on-premise platform. Core domain logic is unchanged."
)


class OnPremKnowledgeBaseAdapter:
    """Placeholder governed-RAG adapter for the on-prem profile."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def ingest(
        self,
        document: KycDocument,
        content: bytes,
        acl_tags: tuple[str, ...],
        page_texts: tuple[str, ...] = (),
    ) -> IngestResult:
        raise NotImplementedError(_MESSAGE)

    def search(self, query: RetrievalQuery) -> list[RetrievedPassage]:
        raise NotImplementedError(_MESSAGE)

    def retract(self, document_id: str, acl_principals: tuple[str, ...]) -> bool:
        # Fail-fast like the rest of this placeholder. Returning False would be the dangerous
        # answer: a caller retracting evidence would be told nothing was indexed, when in truth
        # nothing here is wired at all.
        raise NotImplementedError(_MESSAGE)
