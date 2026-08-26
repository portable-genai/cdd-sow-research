"""Remote-platform knowledge-base adapter — thin HTTP client to A2.

B1's governed RAG store is the shared **A2 Enterprise Knowledge Base**
(``enterprise-knowledge-base``). This adapter implements
:class:`KnowledgeBaseClientPort` by POSTing to A2's ``/v1/ingest`` and ``/v1/search``
endpoints (SPEC §6, A2 contract), so the case's KYC documents are indexed into A2 with
case ACL tags and retrieved via A2 governed search, rather than B1 building its own
backend.

The base URL is read from ``HRZ_KB_URL`` with a localhost default.
"""

from __future__ import annotations

import httpx

from ...config import Settings
from ...domain.errors import CddError
from ...domain.models import (
    Citation,
    IngestResult,
    KycDocument,
    RetrievalQuery,
    RetrievedPassage,
    SourceType,
)
from ...envread import setting_or_default
from . import _s2s

_DEFAULT_URL = "http://localhost:8082"
_TIMEOUT = httpx.Timeout(30.0, connect=5.0)

_SOURCE_TYPE_BY_VALUE = {s.value: s for s in SourceType}


class RemoteKnowledgeBaseError(CddError):
    """Raised when the A2 knowledge-base service returns a non-2xx response."""


class RemoteKnowledgeBaseAdapter:
    """HTTP client for the A2 ``enterprise-knowledge-base`` service."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._base_url = _s2s.validate_base_url(
            setting_or_default("HRZ_KB_URL", _DEFAULT_URL), service=type(self).__name__
        )

    def ingest(
        self,
        document: KycDocument,
        content: bytes,
        acl_tags: tuple[str, ...],
        page_texts: tuple[str, ...] = (),
    ) -> IngestResult:
        """Ingest a case KYC document into A2 with its ACL tags.

        When the extractor recovered page boundaries they are sent alongside the text,
        so A2 can attribute a retrieved passage to its source page.
        """
        payload = {
            "document": {
                "id": document.id,
                "doc_type": document.doc_type.value,
                "uri": document.uri,
                "text": content.decode("utf-8", errors="replace"),
                "pages": list(page_texts),
            },
            "acl_tags": list(acl_tags),
            "source_meta": {"resource": "cdd-sow-research"},
        }
        body = self._post("/v1/ingest", payload)
        return IngestResult(
            document_id=str(body.get("document_id", document.id)),
            chunks=int(body.get("chunks", 0) or 0),
            status=str(body.get("status", "indexed")),
            ok=True,
        )

    def search(self, query: RetrievalQuery) -> list[RetrievedPassage]:
        """Retrieve ACL-filtered passages from A2 for grounding the dossier."""
        payload = {
            "query": query.text,
            "top_k": query.top_k,
            "acl_principals": list(query.acl_principals),
            "filters": dict(query.filters),
        }
        body = self._post("/v1/search", payload)
        return [self._to_passage(item) for item in (body.get("passages") or ())]

    # ----------------------------------------------------------------- helpers
    def retract(self, document_id: str, acl_principals: tuple[str, ...]) -> bool:
        """Ask A2 to remove an indexed document. REFUSES rather than pretending to succeed.

        The remote contract for this does not exist yet: `enterprise-knowledge-base` exposes
        ingest and search, and adding a retraction endpoint is its decision to take, not this
        client's to assume. Returning False here would be the worst of the options -- a caller
        retracting evidence would be told there was nothing indexed, and the passage would stay
        citable. Raising names the gap at the point someone hits it.
        """
        raise NotImplementedError(
            "the A2 knowledge-base service exposes no retraction endpoint, so an indexed "
            "document cannot be withdrawn through this adapter. Refusing rather than reporting "
            "a removal that did not happen."
        )

    def _post(self, path: str, payload: dict) -> dict:
        url = f"{self._base_url}{path}"
        try:
            response = httpx.post(
                url,
                json=payload,
                timeout=_TIMEOUT,
                headers=_s2s.headers(settings=self._settings, base_url=self._base_url),
            )
        except httpx.HTTPError as exc:
            raise RemoteKnowledgeBaseError(f"A2 request to {url} failed: {exc}") from exc
        if response.status_code // 100 != 2:
            raise RemoteKnowledgeBaseError(
                f"A2 {url} returned {response.status_code}: {response.text[:500]}"
            )
        body = response.json()
        return body if isinstance(body, dict) else {}

    @staticmethod
    def _to_passage(item: dict) -> RetrievedPassage:
        raw_citation = item.get("citation") or {}
        source_type = _SOURCE_TYPE_BY_VALUE.get(
            str(raw_citation.get("source_type") or "document"), SourceType.DOCUMENT
        )
        citation = Citation(
            source_id=str(raw_citation.get("source_id", "")),
            source_type=source_type,
            title=str(raw_citation.get("title", "")),
            url=str(raw_citation.get("url", "")),
            page=raw_citation.get("page"),
            snippet=str(raw_citation.get("snippet", "")),
            score=raw_citation.get("score"),
        )
        return RetrievedPassage(
            text=str(item.get("text", "")),
            citation=citation,
            score=float(item.get("score", 0.0) or 0.0),
            acl_tags=tuple(str(t) for t in (item.get("acl_tags") or ())),
        )
