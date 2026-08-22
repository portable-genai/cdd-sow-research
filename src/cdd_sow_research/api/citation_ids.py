"""Server-owned citation reference identifiers.

The identifier contains only case/evidence provenance, never a source URL or credential.
Every use is reauthorized against the current case and document stores; it is a locator,
not an authorization capability. The short-lived browser-flow ticket is the opaque
capability and is persisted only as a digest.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

_PREFIX = "c1."


@dataclass(frozen=True, slots=True)
class CitationReference:
    case_id: str
    evidence_id: str
    source_id: str
    page: int | None

    def __post_init__(self) -> None:
        for name in ("case_id", "evidence_id", "source_id"):
            value = getattr(self, name)
            if not value or len(value) > 256 or any(ord(character) < 0x20 for character in value):
                raise ValueError(f"citation reference {name} is invalid")
        if self.page is not None and (type(self.page) is not int or self.page < 1):
            raise ValueError("citation reference page must be a positive integer")


def encode_citation_reference(reference: CitationReference) -> str:
    payload = json.dumps(
        {
            "case": reference.case_id,
            "evidence": reference.evidence_id,
            "source": reference.source_id,
            "page": reference.page,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return _PREFIX + base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_citation_reference(value: str) -> CitationReference:
    if not isinstance(value, str) or not value.startswith(_PREFIX) or len(value) > 1024:
        raise ValueError("citation identifier is invalid")
    encoded = value.removeprefix(_PREFIX)
    try:
        payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        data: Any = json.loads(payload)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("citation identifier is invalid") from exc
    if not isinstance(data, dict) or set(data) != {"case", "evidence", "source", "page"}:
        raise ValueError("citation identifier is invalid")
    return CitationReference(
        case_id=data["case"],
        evidence_id=data["evidence"],
        source_id=data["source"],
        page=data["page"],
    )


def citation_identifier_from_url(
    url: str,
    *,
    source_id: str,
    page: int | None,
) -> str:
    """Create an identifier only for the server-owned case-document route."""
    parsed = urlsplit(url)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        return ""
    parts = parsed.path.split("/")
    if len(parts) != 6 or parts[:3] != ["", "v1", "cases"] or parts[4] != "documents":
        return ""
    case_id = unquote(parts[3])
    evidence_id = unquote(parts[5])
    if not case_id or not evidence_id or evidence_id != source_id:
        return ""
    return encode_citation_reference(
        CitationReference(
            case_id=case_id,
            evidence_id=evidence_id,
            source_id=source_id,
            page=page,
        )
    )
