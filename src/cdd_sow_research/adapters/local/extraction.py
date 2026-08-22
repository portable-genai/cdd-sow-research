"""Local document-extraction adapter (DocumentExtractionPort) — Document AI stand-in.

SDK-free, deterministic plain-text extraction. If ``pypdf`` is importable and the bytes
look like a PDF, each PDF page becomes one page of text; otherwise the bytes are decoded
as UTF-8 text. When no bytes are supplied (the CLI/agent path hands the adapter an empty
body and relies on the case corpus already indexed in the knowledge base), an empty-text
extract carrying the document's structured fields is returned so the pipeline degrades
honestly rather than fabricating document content. There is no Google emulator for
Document AI, so this path is unconditional and imports no google-cloud package.
"""

from __future__ import annotations

from ...config import Settings
from ...domain.models import DocumentExtract, KycDocument


class LocalDocumentExtractionAdapter:
    """Parse a KYC document's bytes into a :class:`DocumentExtract`, no SDK required."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def extract(self, document: KycDocument, content: bytes, mime_type: str) -> DocumentExtract:
        if content:
            if self._looks_like_pdf(content, mime_type):
                pages = self._extract_pdf_pages(content)
                if pages:
                    return DocumentExtract(
                        document_id=document.id,
                        fields={"doc_type": document.doc_type.value},
                        text="\n\n".join(pages),
                        pages=len(pages),
                        page_texts=tuple(pages),
                    )
            text = content.decode("utf-8", errors="replace")
            return DocumentExtract(
                document_id=document.id,
                fields={"doc_type": document.doc_type.value},
                text=text,
                pages=1,
                page_texts=(text,),
            )
        # No bytes supplied: return an empty-text extract with the structured fields.
        # The dossier is grounded by the case corpus already indexed in the knowledge
        # base, so the adapter does not fabricate document content here.
        return DocumentExtract(
            document_id=document.id,
            fields={"doc_type": document.doc_type.value},
            text="",
            pages=0,
        )

    @staticmethod
    def _looks_like_pdf(content: bytes, mime_type: str) -> bool:
        if "pdf" in (mime_type or "").lower():
            return True
        return isinstance(content, bytes) and content[:5] == b"%PDF-"

    @staticmethod
    def _extract_pdf_pages(content: bytes) -> list[str]:
        """Extract per-page text via pypdf when available; empty list if it is not."""
        try:
            import io

            from pypdf import PdfReader  # type: ignore[import-not-found]
        except Exception:  # noqa: BLE001 - pypdf is optional; fall back to text decode
            return []
        try:
            reader = PdfReader(io.BytesIO(content))
            return [(page.extract_text() or "") for page in reader.pages]
        except Exception:  # noqa: BLE001 - a malformed PDF falls back to text decode
            return []
