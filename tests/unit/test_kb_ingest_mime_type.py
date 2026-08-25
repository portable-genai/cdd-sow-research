"""What the knowledge base tells Discovery Engine each document IS.

Every ingested document was declared ``application/pdf``. A text, CSV or image upload was then
handed to the PDF parser and failed indexing with "Document parsing stage failure: Failed to
parse the PDF file: FILE_READ_ERROR" -- while still LISTING in the data store, carrying an
errored index status that nothing surfaced. Retrieval returned nothing, and the dossier was
refused for want of evidence that had been uploaded, stored and ingested. Three visibly green
steps and a silent fourth.

Sniffed from the content because the port hands the adapter a ``KycDocument``, which carries no
filename and no media type, and because the bytes are what the parser will actually read.
"""

from __future__ import annotations

import pytest

from cdd_sow_research.adapters.gcp.agent_search_kb import _ingest_mime_type

_PDF = b"%PDF-1.7\nfake"
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 8
_WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 4
_WAV = b"RIFF" + b"\x00\x00\x00\x00" + b"WAVE" + b"\x00" * 4


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (_PDF, "application/pdf"),
        (_PNG, "image/png"),
        (_JPEG, "image/jpeg"),
        (_WEBP, "image/webp"),
        (b"MERIDIAN HARBOUR HOLDINGS\nSource of wealth statement\n", "text/plain"),
        (b"name,amount\nA. Tan,12400000\n", "text/plain"),
    ],
    ids=["pdf", "png", "jpeg", "webp", "text", "csv"],
)
def test_the_declared_type_follows_the_bytes(content: bytes, expected: str) -> None:
    assert _ingest_mime_type(content) == expected


def test_a_riff_container_that_is_not_webp_is_not_claimed_as_an_image() -> None:
    """RIFF is also WAV and AVI. Only the WEBP form is a document this store indexes."""

    assert _ingest_mime_type(_WAV) != "image/webp"


def test_unrecognised_binary_falls_back_to_the_previous_behaviour() -> None:
    """The fallback must be PDF: that is what everything was declared as before this existed,
    so an unknown format is no worse off than it was and every known one is now correct."""

    assert _ingest_mime_type(b"\x00\x01\x02\x03\xff\xfe") == "application/pdf"


# --------------------------------------------------------------------------------------- #
# What gets ingested when extraction has nothing to say.
# --------------------------------------------------------------------------------------- #
def test_a_document_that_is_already_text_is_ingested_as_itself() -> None:
    """Extraction is built for scanned and laid-out documents.

    For a plain text, CSV or Markdown upload it returns nothing, which is the honest answer
    for an extractor and the wrong thing to hand a knowledge base: the text was there all
    along. Ingesting the empty result had the store accept the document and then report "the
    parsed result is empty" in an index status nothing surfaces, so retrieval found no evidence
    for a case whose evidence had been uploaded and stored.
    """

    from cdd_sow_research.domain.cdd_service import _own_text

    assert _own_text(b"MERIDIAN HARBOUR HOLDINGS\nSource of wealth statement\n").startswith(
        b"MERIDIAN"
    )


def test_binary_a_text_extractor_could_not_read_is_not_passed_off_as_text() -> None:
    from cdd_sow_research.domain.cdd_service import _own_text

    assert _own_text(b"%PDF-1.7\n\x80\x81\x82") == b""
    assert _own_text(b"") == b""
