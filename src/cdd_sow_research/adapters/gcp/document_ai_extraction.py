"""Document AI extraction adapter (DocumentExtractionPort).

Primary production extraction backend for B1: **Document AI** on the Gemini Enterprise
Agent Platform, pinned to a **regional** processor in ``asia-southeast1`` (Singapore) so
that KYC document processing stays in-country for residency. The adapter calls the
processor's ``process_document`` and maps the result onto a domain
:class:`DocumentExtract` (form-field key/values plus the full text and page count).

All Google Cloud SDK imports are lazy (inside ``__init__`` / methods) so the on-prem /
test profile imports this module without ``google-cloud-documentai`` installed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ...config import Settings
from ...domain.models import DocumentExtract, KycDocument

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from google.cloud import documentai_v1


class DocumentAiExtractionAdapter:
    """Extract structured fields + text from a KYC document via Document AI."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        cfg = settings.document_ai
        self._location = cfg.location
        self._processor_id = cfg.processor_id
        self._processor_version = cfg.processor_version
        # Regional endpoint; the global endpoint gives no residency guarantee.
        self._endpoint = f"{self._location}-documentai.googleapis.com"
        self._client: Any | None = None

    def _get_client(self) -> documentai_v1.DocumentProcessorServiceClient:
        if self._client is None:
            from google.api_core.client_options import ClientOptions
            from google.cloud import documentai_v1

            self._client = documentai_v1.DocumentProcessorServiceClient(
                client_options=ClientOptions(api_endpoint=self._endpoint),
            )
        return self._client

    def extract(self, document: KycDocument, content: bytes, mime_type: str) -> DocumentExtract:
        """Process ``content`` with Document AI and map to a domain extract."""
        from google.cloud import documentai_v1

        client = self._get_client()
        name = (
            f"projects/{self._settings.project_id}/locations/{self._location}"
            f"/processors/{self._processor_id}"
        )
        raw_document = documentai_v1.RawDocument(content=content, mime_type=mime_type)
        request = documentai_v1.ProcessRequest(name=name, raw_document=raw_document)
        result = client.process_document(request=request)
        return self._to_extract(document.id, result.document)

    @staticmethod
    def _to_extract(document_id: str, doc: Any) -> DocumentExtract:
        text = getattr(doc, "text", "") or ""
        pages = list(getattr(doc, "pages", []) or [])
        fields: dict[str, str] = {}
        for page in pages:
            for form_field in getattr(page, "form_fields", []) or []:
                key = DocumentAiExtractionAdapter._anchor_text(form_field.field_name, text)
                value = DocumentAiExtractionAdapter._anchor_text(form_field.field_value, text)
                if key:
                    fields[key] = value
        return DocumentExtract(document_id=document_id, fields=fields, text=text, pages=len(pages))

    @staticmethod
    def _anchor_text(layout: Any, full_text: str) -> str:
        """Resolve a Document AI text-anchor into the substring it points at."""
        anchor = getattr(getattr(layout, "text_anchor", None), "text_segments", None)
        if not anchor:
            return ""
        out: list[str] = []
        for segment in anchor:
            start = int(getattr(segment, "start_index", 0) or 0)
            end = int(getattr(segment, "end_index", 0) or 0)
            out.append(full_text[start:end])
        return "".join(out).strip()
