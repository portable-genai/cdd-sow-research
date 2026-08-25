"""Agent Search knowledge-base adapter (KnowledgeBaseClientPort, standalone).

B1's governed RAG store **is** the A2 Enterprise KB; the ``platform`` adapter delegates
to A2 over HTTP. For a standalone run this adapter speaks directly to **Agent Search**
(Discovery Engine) on the Gemini Enterprise Agent Platform, pinned to a **regional**
endpoint in ``asia-southeast1`` so case documents stay in-country. ``ingest`` imports a
KYC document into the case data store with its ACL tags; ``search`` returns ranked
passages with page-level :class:`Citation` provenance.

All Google Cloud SDK imports are lazy so the on-prem / test profile imports this module
without ``google-cloud-discoveryengine`` installed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ...config import Settings
from ...domain.models import (
    Citation,
    IngestResult,
    KycDocument,
    RetrievalQuery,
    RetrievedPassage,
    SourceType,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from google.cloud import discoveryengine_v1


#: Leading bytes that identify a format, longest first so a prefix cannot shadow a longer one.
#:
#: The declared type was hardcoded to ``application/pdf`` for every document, so a text, CSV or
#: image upload was handed to the PDF parser and failed indexing with "Document parsing stage
#: failure: Failed to parse the PDF file: FILE_READ_ERROR". The document still LISTED in the data
#: store, carrying an errored index status that nothing surfaced, so retrieval returned nothing
#: and the dossier was refused for want of evidence that had in fact been uploaded, stored and
#: ingested. Three green steps and a silent fourth.
#:
#: Sniffed from the CONTENT rather than declared, because the port hands this adapter a
#: :class:`KycDocument`, which carries no filename and no media type -- and because the bytes are
#: the thing the parser will actually read, so they are the honest source for what it is.
_CONTENT_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"RIFF", "image/webp"),
)


def _ingest_mime_type(content: bytes) -> str:
    """What Discovery Engine should PARSE these bytes as.

    Unrecognised binary falls back to PDF, which is what everything was declared as before, so
    nothing is worse off than it was and everything recognisable is now parsed correctly.
    """

    for signature, mime_type in _CONTENT_SIGNATURES:
        if content.startswith(signature):
            # RIFF is also WAV and AVI; only the WEBP form is a document this store indexes.
            if signature == b"RIFF" and content[8:12] != b"WEBP":
                continue
            return mime_type
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        return "application/pdf"
    return "text/plain"


class AgentSearchKnowledgeBaseAdapter:
    """Direct Agent Search governed-RAG adapter (standalone fallback for A2)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        cfg = settings.knowledge_base
        self._location = cfg.location
        self._engine_id = cfg.engine_id
        self._data_store_id = cfg.data_store_id
        self._top_k = cfg.top_k
        self._collection_id = cfg.collection_id
        self._branch_id = cfg.branch_id
        self._serving_config_id = cfg.serving_config_id
        self._endpoint = f"{self._location}-discoveryengine.googleapis.com"
        self._search_client: Any | None = None
        self._doc_client: Any | None = None

    # ------------------------------------------------------------------ #
    # Lazy clients
    # ------------------------------------------------------------------ #
    def _search(self) -> discoveryengine_v1.SearchServiceClient:
        if self._search_client is None:
            from google.api_core.client_options import ClientOptions
            from google.cloud import discoveryengine_v1

            self._search_client = discoveryengine_v1.SearchServiceClient(
                client_options=ClientOptions(api_endpoint=self._endpoint),
            )
        return self._search_client

    def _documents(self) -> discoveryengine_v1.DocumentServiceClient:
        if self._doc_client is None:
            from google.api_core.client_options import ClientOptions
            from google.cloud import discoveryengine_v1

            self._doc_client = discoveryengine_v1.DocumentServiceClient(
                client_options=ClientOptions(api_endpoint=self._endpoint),
            )
        return self._doc_client

    def _branch(self) -> str:
        return (
            f"projects/{self._settings.project_id}/locations/{self._location}"
            f"/collections/{self._collection_id}/dataStores/{self._data_store_id}"
            f"/branches/{self._branch_id}"
        )

    def _serving_config(self) -> str:
        """The serving config to search: the ENGINE's when one is configured.

        A data store's own serving config is Standard edition, and this adapter asks for
        extractive segments, which is an Enterprise-edition feature. Searching the data store
        directly therefore fails with a 400 telling the caller, in so many words, to address the
        engine instead. Ingestion still targets the data store, because documents are written to
        a branch and an engine has none.
        """

        base = f"projects/{self._settings.project_id}/locations/{self._location}"
        if self._engine_id:
            return (
                f"{base}/collections/{self._collection_id}/engines/{self._engine_id}"
                f"/servingConfigs/{self._serving_config_id}"
            )
        return (
            f"{base}/collections/{self._collection_id}/dataStores/{self._data_store_id}"
            f"/servingConfigs/{self._serving_config_id}"
        )

    # ------------------------------------------------------------------ #
    # KnowledgeBaseClientPort
    # ------------------------------------------------------------------ #
    def ingest(
        self,
        document: KycDocument,
        content: bytes,
        acl_tags: tuple[str, ...],
        page_texts: tuple[str, ...] = (),
    ) -> IngestResult:
        """Index a KYC document into the case data store with its ACL tags.

        Agent Search does its own layout-aware chunking over the raw bytes, so
        ``page_texts`` is recorded as structured metadata (the page count) rather than
        used to pre-split the document.
        """
        from google.cloud import discoveryengine_v1

        client = self._documents()
        struct = {
            "source_id": document.id,
            "doc_type": document.doc_type.value,
            "uri": document.uri,
            "acl_tags": list(acl_tags),
            "pages": len(page_texts),
        }
        doc = discoveryengine_v1.Document(
            id=document.id,
            struct_data=struct,
            content=discoveryengine_v1.Document.Content(
                raw_bytes=content, mime_type=_ingest_mime_type(content)
            ),
        )
        client.create_document(parent=self._branch(), document=doc, document_id=document.id)
        return IngestResult(document_id=document.id, chunks=0, status="indexed", ok=True)

    def search(self, query: RetrievalQuery) -> list[RetrievedPassage]:
        """Return ranked passages (ACL-filtered) with page-level citations."""
        from google.cloud import discoveryengine_v1

        client = self._search()
        content_spec = discoveryengine_v1.SearchRequest.ContentSearchSpec(
            extractive_content_spec=(
                discoveryengine_v1.SearchRequest.ContentSearchSpec.ExtractiveContentSpec(
                    max_extractive_segment_count=max(query.top_k, 1),
                    return_extractive_segment_score=True,
                )
            ),
        )
        request = discoveryengine_v1.SearchRequest(
            serving_config=self._serving_config(),
            query=query.text,
            # Over-fetch by relevance, then enforce the ACL in Python (below). A
            # server-side Discovery Engine ACL filter is deliberately NOT used: a subset
            # (all-of) match is not simply expressible in its filter grammar, and an
            # any-overlap filter would wrongly drop untagged (public) docs whose acl_tags
            # field is absent. This mirrors the local adapter (SQL by rank, Python ACL).
            page_size=max(query.top_k, 1) * 4,
            content_search_spec=content_spec,
        )
        response = client.search(request=request)
        passages: list[RetrievedPassage] = []
        for result in response.results:
            passages.extend(self._result_to_passages(result))
        # Authoritative ACL enforcement (ports/knowledge_base.py contract): a tagged
        # passage is visible only when the query holds EVERY tag on it (subset, all-of),
        # untagged passages are public, and an empty principal set sees only untagged
        # ones. Identical semantics to the local adapter's _acl_ok, fail-closed.
        visible = [p for p in passages if self._acl_ok(p.acl_tags, query.acl_principals)]
        return visible[: max(query.top_k, 1)]

    # ------------------------------------------------------------------ #
    # Mapping helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _acl_ok(passage_tags: tuple[str, ...], acl_principals: tuple[str, ...]) -> bool:
        """Subset, fail-closed: untagged is public, else the query must hold every tag."""
        if not passage_tags:
            return True
        return set(passage_tags) <= set(acl_principals)

    def _result_to_passages(self, result: Any) -> list[RetrievedPassage]:
        document = result.document
        struct = self._to_dict(getattr(document, "struct_data", None))
        derived = self._to_dict(getattr(document, "derived_struct_data", None))
        source_id = str(struct.get("source_id") or getattr(document, "id", "") or "unknown")
        title = str(struct.get("source_id") or source_id)
        url = str(struct.get("uri") or "")
        acl_tags = tuple(str(t) for t in (struct.get("acl_tags") or ()))

        segments = derived.get("extractive_segments") or []
        if not segments:
            citation = Citation(
                source_id=source_id,
                source_type=SourceType.DOCUMENT,
                title=title,
                url=url,
                page=None,
            )
            return [RetrievedPassage(text="", citation=citation, score=0.0, acl_tags=acl_tags)]

        passages: list[RetrievedPassage] = []
        for segment in segments:
            seg = self._to_dict(segment)
            text = str(seg.get("content") or "")
            page = self._parse_page(seg.get("pageIdentifier"))
            score = self._parse_score(seg.get("relevanceScore"))
            citation = Citation(
                source_id=source_id,
                source_type=SourceType.DOCUMENT,
                title=title,
                url=url,
                page=page,
                snippet=text[:280],
                score=score,
            )
            passages.append(
                RetrievedPassage(
                    text=text, citation=citation, score=score or 0.0, acl_tags=acl_tags
                )
            )
        return passages

    @staticmethod
    def _to_dict(value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if isinstance(value, dict):
            return value
        try:
            return dict(value)
        except (TypeError, ValueError):
            return {}

    @staticmethod
    def _parse_page(value: Any) -> int | None:
        if value is None:
            return None
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parse_score(value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
