"""The live extractor: real PDF text layers, and the bounded vision fallback.

Every case here runs against genuine PDF bytes, so the page split under test is the one
a real document produces. The vision model is stubbed (the point is the routing and the
budget, not the model), and the assertions cover what a reviewer depends on: correct page
attribution, and a visible marker wherever a page could not be read at all.
"""

from __future__ import annotations

import pytest
from tests.fixtures.pdfs import BANK_STATEMENT_PAGES, build_pdf

from cdd_sow_research.adapters.live import extraction as live_extraction
from cdd_sow_research.adapters.live.extraction import (
    UNREADABLE_MARKER,
    LiveDocumentExtractionAdapter,
)
from cdd_sow_research.config import LiveSettings, Settings
from cdd_sow_research.domain.models import DocType, KycDocument

_DOCUMENT = KycDocument(id="doc-1", doc_type=DocType.BANK_STATEMENT)


def _adapter(**live: object) -> LiveDocumentExtractionAdapter:
    return LiveDocumentExtractionAdapter(Settings(profile="live", live=LiveSettings(**live)))


class _StubClient:
    """Stands in for the local model server; records what it was asked to transcribe."""

    def __init__(self, reply: str = "TRANSCRIBED PAGE", fail: bool = False) -> None:
        self.reply = reply
        self.fail = fail
        self.calls = 0

    def chat(self, messages, model, temperature=0.0, max_tokens=0):  # noqa: ANN001, ANN201
        self.calls += 1
        if self.fail:
            raise live_extraction.LocalModelError("server down")
        return self.reply, {"input_tokens": 1, "output_tokens": 1}, model


def test_a_digital_pdf_is_read_page_by_page_with_no_model_involved():
    stub = _StubClient()
    adapter = _adapter()
    adapter._client = stub  # type: ignore[attr-defined]

    extract = adapter.extract(_DOCUMENT, build_pdf(BANK_STATEMENT_PAGES), "application/pdf")

    assert extract.pages == 2
    assert len(extract.page_texts) == 2
    # Page attribution is what makes a citation checkable: the property sale is on p.2.
    assert "Opening balance" in extract.page_texts[0]
    assert "21 Orchard Rise" in extract.page_texts[1]
    assert "21 Orchard Rise" not in extract.page_texts[0]
    assert stub.calls == 0, "a readable text layer must not spend a vision call"


def test_a_page_with_no_text_layer_is_transcribed_by_the_vision_model():
    stub = _StubClient(reply="SCANNED: closing balance SGD 3,918,442.60")
    adapter = _adapter()
    adapter._client = stub  # type: ignore[attr-defined]
    # Page 2 is blank (a scan): no text layer, so it must go to the vision path.
    pdf = build_pdf([BANK_STATEMENT_PAGES[0], []])

    extract = adapter.extract(_DOCUMENT, pdf, "application/pdf")

    assert stub.calls == 1
    assert "Opening balance" in extract.page_texts[0]
    assert extract.page_texts[1] == "SCANNED: closing balance SGD 3,918,442.60"


def test_transcription_is_capped_by_the_per_document_budget():
    stub = _StubClient()
    adapter = _adapter(max_ocr_pages=2)
    adapter._client = stub  # type: ignore[attr-defined]
    pdf = build_pdf([[], [], [], []])  # four unreadable pages

    extract = adapter.extract(_DOCUMENT, pdf, "application/pdf")

    assert stub.calls == 2, "the budget bounds how long one document can stall an assessment"
    assert extract.page_texts[2] == UNREADABLE_MARKER
    assert extract.page_texts[3] == UNREADABLE_MARKER


def test_a_skipped_render_never_shifts_a_transcription_onto_another_page(monkeypatch):
    """Rendering is keyed by page index, not position.

    If a render is skipped (the libraries can disagree on page count on a malformed
    file, and a render can fail mid-document), a positional zip would attribute one
    page's text to a different page: a citation pointing at the wrong evidence.
    """
    stub = _StubClient(reply="TEXT OF PAGE 4")
    adapter = _adapter()
    adapter._client = stub  # type: ignore[attr-defined]
    pdf = build_pdf([BANK_STATEMENT_PAGES[0], [], [], []])  # pages 2, 3, 4 unreadable

    # Only the last requested page renders; the earlier two are skipped.
    real_render = adapter._render_pages

    def only_the_last(content, indexes):  # noqa: ANN001, ANN202
        rendered = real_render(content, indexes)
        return {max(rendered): rendered[max(rendered)]} if rendered else {}

    monkeypatch.setattr(adapter, "_render_pages", only_the_last)

    extract = adapter.extract(_DOCUMENT, pdf, "application/pdf")

    assert extract.page_texts[3] == "TEXT OF PAGE 4", "the rendered page keeps its own text"
    assert extract.page_texts[1] == UNREADABLE_MARKER
    assert extract.page_texts[2] == UNREADABLE_MARKER


def test_a_page_the_model_cannot_read_is_marked_not_silently_blank():
    adapter = _adapter()
    adapter._client = _StubClient(fail=True)  # type: ignore[attr-defined]

    extract = adapter.extract(_DOCUMENT, build_pdf([[]]), "application/pdf")

    assert extract.page_texts == (UNREADABLE_MARKER,)


def test_ocr_can_be_switched_off_entirely():
    stub = _StubClient()
    adapter = _adapter(ocr_enabled=False)
    adapter._client = stub  # type: ignore[attr-defined]

    extract = adapter.extract(_DOCUMENT, build_pdf([[]]), "application/pdf")

    assert stub.calls == 0
    assert extract.page_texts == (UNREADABLE_MARKER,)


def test_an_uploaded_image_goes_straight_to_the_vision_model():
    stub = _StubClient(reply="REGISTRY EXTRACT")
    adapter = _adapter()
    adapter._client = stub  # type: ignore[attr-defined]

    extract = adapter.extract(_DOCUMENT, b"\x89PNG\r\n\x1a\n fake", "image/png")

    assert stub.calls == 1
    assert extract.page_texts == ("REGISTRY EXTRACT",)


def test_plain_text_is_taken_as_written():
    adapter = _adapter()
    extract = adapter.extract(_DOCUMENT, b"Dividend income SGD 318,500", "text/plain")

    assert extract.text == "Dividend income SGD 318,500"
    assert extract.pages == 1


def test_no_bytes_yields_an_empty_extract_rather_than_invented_content():
    adapter = _adapter()
    extract = adapter.extract(_DOCUMENT, b"", "application/pdf")

    assert extract.text == ""
    assert extract.pages == 0
    assert extract.page_texts == ()


@pytest.mark.parametrize("mime", ["application/pdf", "", "application/octet-stream"])
def test_pdf_bytes_are_recognised_however_they_are_labelled(mime: str):
    adapter = _adapter()
    adapter._client = _StubClient()  # type: ignore[attr-defined]

    extract = adapter.extract(_DOCUMENT, build_pdf(BANK_STATEMENT_PAGES), mime)

    assert extract.pages == 2
