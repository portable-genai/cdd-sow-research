"""Jurisdiction-driven PII redaction (compliance packs).

Proves a non-SG deployment detects its own national identifiers by configuration, not a
code change, and that the default reproduces the Singapore behavior. This is what keeps a
forked repo's ``pii_safety`` gate honest instead of falsely green.
"""

from __future__ import annotations

from cdd_sow_research.adapters.local.redaction import LocalRegexRedactionAdapter
from cdd_sow_research.config import PiiSettings, Settings
from cdd_sow_research.domain import pii_patterns


def _redactor(*jurisdictions: str) -> LocalRegexRedactionAdapter:
    return LocalRegexRedactionAdapter(Settings(pii=PiiSettings(jurisdictions=jurisdictions)))


def test_default_sg_masks_nric_and_email() -> None:
    r = _redactor("SG")
    out = r.redact("NRIC S1234567A, email ops@example.com")
    assert "S1234567A" not in out.text
    assert "ops@example.com" not in out.text
    info = {f.info_type for f in out.findings}
    assert {"SG_NRIC_FIN", "EMAIL_ADDRESS"} <= info


def test_india_pack_masks_pan_and_aadhaar_but_sg_default_does_not() -> None:
    text = "PAN ABCDE1234F and Aadhaar 1234 5678 9012 on file."
    sg = _redactor("SG").redact(text)
    india = _redactor("IN").redact(text)
    # The SG pack does not know Indian identifiers: they survive (the false-green risk).
    assert "ABCDE1234F" in sg.text
    # The IN pack detects and masks them.
    assert "ABCDE1234F" not in india.text
    assert "1234 5678 9012" not in india.text
    info = {f.info_type for f in india.findings}
    assert "IN_PAN" in info and "IN_AADHAAR" in info


def test_multiple_jurisdictions_compose() -> None:
    r = _redactor("SG", "GB")
    out = r.redact("SG NRIC S1234567A and UK NINO AB123456C.")
    assert "S1234567A" not in out.text
    assert "AB123456C" not in out.text


def test_patterns_always_include_universal_email() -> None:
    for js in (("SG",), ("IN",), ("XX",)):  # unknown code still gets email/phone
        info_types = {info for info, _ in pii_patterns.patterns_for(js)}
        assert "EMAIL_ADDRESS" in info_types
