"""Local PII redaction adapter (PIIRedactionPort) — regex de-identification.

The ``local`` profile's stand-in for **Sensitive Data Protection / DLP**: masks the
national identifiers for the configured jurisdiction(s) plus universal email/phone with
deterministic regexes, returning findings. This is the redact-before-everything boundary
(P-04, R1) so customer PII never reaches the model, a trace span or the audit sink. The
pattern set is jurisdiction-driven (``settings.pii.jurisdictions``, default ``["SG"]``) so
a non-SG deployment detects its own identifiers (PAN, Aadhaar, NINO, NIK, ...) by config,
not a code change (see ``domain/pii_patterns.py``). There is no Google emulator for DLP,
so this path is unconditional and imports no google-cloud package.
"""

from __future__ import annotations

from ...config import Settings
from ...domain.models import RedactionFinding, RedactionResult
from ...domain.pii_patterns import patterns_for


class LocalRegexRedactionAdapter:
    """Mask the configured jurisdictions' national ids + email/phone, like DLP de-identify."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        jurisdictions = getattr(getattr(settings, "pii", None), "jurisdictions", ("SG",))
        self._patterns = patterns_for(jurisdictions)

    def redact(self, text: str) -> RedactionResult:
        findings: list[RedactionFinding] = []
        redacted = text
        for info_type, pattern in self._patterns:
            hits = pattern.findall(redacted)
            if hits:
                redacted = pattern.sub(f"[{info_type}]", redacted)
                findings.append(RedactionFinding(info_type=info_type, count=len(hits)))
        return RedactionResult(text=redacted, findings=tuple(findings))
