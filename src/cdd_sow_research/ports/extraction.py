"""DocumentExtractionPort — structured extraction from KYC documents.

Primary GCP adapter: **Document AI** on the Gemini Enterprise Agent Platform, pinned
to a single in-country region. It turns a raw KYC document (passport, financial
statement, registry extract, bank statement) into a :class:`DocumentExtract` of form
fields plus full text. On-prem migration swaps this for a placeholder adapter with no
change to callers.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..domain.models import DocumentExtract, KycDocument


@runtime_checkable
class DocumentExtractionPort(Protocol):
    def extract(self, document: KycDocument, content: bytes, mime_type: str) -> DocumentExtract:
        """Extract structured fields and text from a KYC document's bytes."""
        ...
