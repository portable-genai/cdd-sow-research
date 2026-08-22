"""Live document-extraction adapter (DocumentExtractionPort) — real files, on-machine.

Reads what an analyst actually uploads: PDFs (digital or scanned), page images, and
plain text. Every path runs on the operator's own machine, so a customer's bank
statement is never shipped to a third party to be read.

How a PDF is handled, page by page:

1. ``pypdf`` recovers the text layer. For a digitally produced document that is the
   whole job: exact text, correct page numbers, no model involved.
2. A page whose text layer is missing or too thin to be evidence (a scan, a photo) is
   rendered to an image with ``pypdfium2`` and transcribed by the local vision model.
   This is the OCR path, and it is bounded: at most ``max_ocr_pages`` pages per
   document, so one 400-page scan cannot stall an assessment.
3. A page that neither route can read is recorded as an explicit unreadable marker
   rather than silently becoming an empty page, so a reviewer can see the gap instead
   of inferring that the page was blank.

Page boundaries are preserved in ``DocumentExtract.page_texts``, which is what lets the
knowledge base cite "p.11" and the UI deep-link the reviewer to that exact page.
"""

from __future__ import annotations

import io
import logging

from ...config import Settings
from ...domain.models import DocumentExtract, KycDocument
from ._client import LocalModelError, OpenAiCompatClient, image_part, text_part

_LOG = logging.getLogger(__name__)

#: A page with less real text than this is treated as needing the vision path. Page
#: furniture (a header, a page number) clears a character count but is not evidence.
_MIN_TEXT_CHARS = 40

_TRANSCRIBE_PROMPT = (
    "Transcribe this document page into plain text. Preserve the reading order, "
    "headings, line items, dates and every number exactly as printed, including "
    "currency symbols and separators. Reproduce tables as one line per row with "
    "columns separated by ' | '. Do not summarise, translate, correct or explain "
    "anything, and do not add any commentary. If the page is blank, answer exactly: "
    "[blank page]"
)

#: Recorded in place of a page that could not be read at all. Deliberately visible in
#: the evidence: an unreadable page is a gap a reviewer must know about.
UNREADABLE_MARKER = "[unreadable page: no text layer and no transcription available]"

_IMAGE_MIMES = ("image/png", "image/jpeg", "image/jpg", "image/webp", "image/tiff")


class LiveDocumentExtractionAdapter:
    """Extract real uploaded documents locally: text layer first, vision as fallback."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        live = settings.live
        self._client = OpenAiCompatClient(live.llm_url, live.timeout_seconds)
        self._vision_model = live.vision_model or live.llm_model
        self._max_ocr_pages = live.max_ocr_pages
        self._render_dpi = live.render_dpi
        self._ocr_enabled = live.ocr_enabled

    # ------------------------------------------------------------------ #
    # DocumentExtractionPort
    # ------------------------------------------------------------------ #
    def extract(self, document: KycDocument, content: bytes, mime_type: str) -> DocumentExtract:
        if not content:
            # No bytes: the dossier is grounded by whatever is already indexed for the
            # case. Never fabricate document content here.
            return self._extract_result(document, [])

        mime = (mime_type or "").lower()
        if self._looks_like_pdf(content, mime):
            return self._extract_result(document, self._pdf_pages(content))
        if any(mime.startswith(m) for m in _IMAGE_MIMES):
            return self._extract_result(document, [self._transcribe(content, page_no=1)])
        return self._extract_result(document, [content.decode("utf-8", errors="replace")])

    # ------------------------------------------------------------------ #
    # PDF
    # ------------------------------------------------------------------ #
    def _pdf_pages(self, content: bytes) -> list[str]:
        """Per-page text: the text layer where it exists, vision transcription where not."""
        text_pages = self._text_layer(content)
        if not text_pages:
            # Not parseable as a PDF at all: try the vision path on a single render, so
            # a malformed-but-renderable file still yields evidence.
            rendered = self._render_pages(content, [0])
            if 0 in rendered:
                return [self._transcribe(rendered[0], page_no=1)]
            return []

        thin = [i for i, page in enumerate(text_pages) if len(page.strip()) < _MIN_TEXT_CHARS]
        if not thin or not self._ocr_enabled:
            return [p if p.strip() else UNREADABLE_MARKER for p in text_pages]

        budget = thin[: self._max_ocr_pages]
        if len(thin) > len(budget):
            _LOG.warning(
                "document has %d pages needing transcription; transcribing the first %d "
                "(live.max_ocr_pages)",
                len(thin),
                len(budget),
            )
        rendered = self._render_pages(content, budget)
        out = list(text_pages)
        for index, image in rendered.items():
            out[index] = self._transcribe(image, page_no=index + 1)
        return [p if p.strip() else UNREADABLE_MARKER for p in out]

    @staticmethod
    def _text_layer(content: bytes) -> list[str]:
        """Per-page text from the PDF's own text layer (empty list if unparseable)."""
        try:
            from pypdf import PdfReader
        except ImportError:  # pragma: no cover - pypdf ships with the live extra
            _LOG.warning("pypdf is not installed; cannot read PDF text layers")
            return []
        try:
            reader = PdfReader(io.BytesIO(content))
            return [(page.extract_text() or "") for page in reader.pages]
        except Exception as exc:  # noqa: BLE001 - a malformed PDF falls back to vision
            _LOG.warning("could not read PDF text layer: %s", exc)
            return []

    def _render_pages(self, content: bytes, indexes: list[int]) -> dict[int, bytes]:
        """Render the given zero-based page indexes, keyed BY page index.

        Keyed, not a positional list, because a page can be skipped: the two libraries
        can disagree on the page count of a malformed file, and a render can fail
        mid-document. Zipping a shortened list back against the requested indexes would
        then attribute one page's transcription to a different page, which is a citation
        pointing at the wrong evidence: the worst failure this pipeline has.
        """
        try:
            import pypdfium2
        except ImportError:  # pragma: no cover - pypdfium2 ships with the live extra
            _LOG.warning("pypdfium2 is not installed; cannot render pages for transcription")
            return {}
        images: dict[int, bytes] = {}
        try:
            pdf = pypdfium2.PdfDocument(content)
            try:
                scale = self._render_dpi / 72.0
                for index in indexes:
                    if index >= len(pdf):
                        continue
                    bitmap = pdf[index].render(scale=scale)
                    buffer = io.BytesIO()
                    bitmap.to_pil().save(buffer, format="PNG")
                    images[index] = buffer.getvalue()
            finally:
                pdf.close()
        except Exception as exc:  # noqa: BLE001 - rendering is a fallback, never fatal
            _LOG.warning("could not render PDF pages: %s", exc)
        return images

    # ------------------------------------------------------------------ #
    # Vision
    # ------------------------------------------------------------------ #
    def _transcribe(self, image: bytes, page_no: int) -> str:
        """Transcribe one page image with the local vision model."""
        if not self._ocr_enabled:
            return UNREADABLE_MARKER
        messages = [
            {
                "role": "user",
                "content": [image_part(image), text_part(_TRANSCRIBE_PROMPT)],
            }
        ]
        try:
            content, _, _ = self._client.chat(
                messages,
                model=self._vision_model,
                temperature=0.0,
                max_tokens=self._settings.live.max_output_tokens,
            )
        except LocalModelError as exc:
            _LOG.warning("page %d could not be transcribed: %s", page_no, exc)
            return UNREADABLE_MARKER
        text = content.strip()
        if not text or text == "[blank page]":
            return UNREADABLE_MARKER
        return text

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _looks_like_pdf(content: bytes, mime_type: str) -> bool:
        if "pdf" in (mime_type or "").lower():
            return True
        return content[:5] == b"%PDF-"

    @staticmethod
    def _extract_result(document: KycDocument, pages: list[str]) -> DocumentExtract:
        return DocumentExtract(
            document_id=document.id,
            fields={"doc_type": document.doc_type.value},
            text="\n\n".join(pages),
            pages=len(pages),
            page_texts=tuple(pages),
        )
