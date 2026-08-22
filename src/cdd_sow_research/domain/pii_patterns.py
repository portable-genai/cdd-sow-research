"""Jurisdiction-driven PII pattern packs (the compliance-pack PII axis).

The redact-before-everything boundary (P-04, R1) and the eval gate's ``pii_safety`` metric
both need to know what a national identifier LOOKS like, and that is jurisdiction-specific:
a bank in India must detect PAN / Aadhaar, one in the UK a National Insurance number, one
in Indonesia a NIK, not just a Singapore NRIC. Keeping the patterns here (pure stdlib) and
selecting by jurisdiction means a non-SG fork changes a config list, not code, and its
``pii_safety`` gate actually exercises its own identifiers instead of being falsely green.

Add a jurisdiction by adding a ``(info_type, regex)`` row to :data:`NATIONAL_ID_PATTERNS`.
``EMAIL`` and ``PHONE`` are universal and always applied. The regexes are deliberately
conservative (structural shape, not checksum-validated): the goal is redaction recall at
a boundary, not authoritative identity validation.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# Universal PII, applied for every jurisdiction.
_EMAIL = ("EMAIL_ADDRESS", re.compile(r"\b[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}\b"))
_PHONE_INTL = ("PHONE_NUMBER", re.compile(r"\+\d{1,3}[\s-]?\d(?:[\s-]?\d){6,13}\b"))

#: Per-jurisdiction national-identifier patterns (ISO-3166 alpha-2 -> list of rows).
NATIONAL_ID_PATTERNS: dict[str, list[tuple[str, re.Pattern[str]]]] = {
    "SG": [
        ("SG_NRIC_FIN", re.compile(r"\b[STFGM]\d{7}[A-Z]\b")),
        ("SG_PHONE", re.compile(r"\b(?:\+?65[\s-]?)?[689]\d{3}[\s-]?\d{4}\b")),
    ],
    "IN": [
        ("IN_PAN", re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")),
        ("IN_AADHAAR", re.compile(r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}\b")),
    ],
    "GB": [
        # National Insurance number (prefix rules simplified to a safe superset).
        ("GB_NINO", re.compile(r"\b[A-CEGHJ-PR-TW-Z]{2}\d{6}[A-D]\b")),
    ],
    "HK": [
        ("HK_HKID", re.compile(r"\b[A-Z]{1,2}\d{6}\([0-9A]\)")),
    ],
    "ID": [
        ("ID_NIK", re.compile(r"\b\d{16}\b")),
    ],
    "MY": [
        ("MY_NRIC", re.compile(r"\b\d{6}[\s-]?\d{2}[\s-]?\d{4}\b")),
    ],
    "AU": [
        ("AU_TFN", re.compile(r"\b\d{3}[\s-]?\d{3}[\s-]?\d{3}\b")),
    ],
}

#: Reference default: the build's home jurisdiction (MAS / Singapore).
DEFAULT_JURISDICTIONS: tuple[str, ...] = ("SG",)


def patterns_for(jurisdictions: Iterable[str]) -> list[tuple[str, re.Pattern[str]]]:
    """The redaction pattern rows for ``jurisdictions`` plus the universal email/phone.

    Unknown jurisdiction codes contribute no national-ID pattern (email/phone still apply),
    so a partially-configured fork degrades safely rather than raising.
    """
    rows: list[tuple[str, re.Pattern[str]]] = [_EMAIL, _PHONE_INTL]
    seen = {info for info, _ in rows}
    for code in jurisdictions:
        for info_type, pattern in NATIONAL_ID_PATTERNS.get((code or "").upper(), ()):
            if info_type not in seen:
                rows.append((info_type, pattern))
                seen.add(info_type)
    return rows


def all_patterns() -> list[tuple[str, re.Pattern[str]]]:
    """Every known pattern across all jurisdictions (used by the eval gate's leak check)."""
    return patterns_for(NATIONAL_ID_PATTERNS.keys())
