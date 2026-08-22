"""On-prem placeholder for ``PIIRedactionPort`` — the sovereign target.

One of the reversibility (P-02, P-12) migration placeholders: in the managed profile
this port binds to the Sensitive Data Protection / DLP adapter; switching ``profile`` to
``onprem`` rebinds it here. The adapter constructs cleanly with **no external
dependencies** and structurally satisfies the same Protocol as the managed adapter, so
the contract tests prove interface parity. ``redact`` deliberately raises rather than
passing text through unredacted: an unimplemented redactor must never leak customer PII
to a model or audit sink (P-04, rule R1), so porting on-premise *must* supply a real
de-identifier. Filling this body in is the only change required.
"""

from __future__ import annotations

from ...config import Settings
from ...domain.models import RedactionResult

_MESSAGE = (
    "On-prem PIIRedactionPort adapter is a migration placeholder; implement against your "
    "on-premise platform. Core domain logic is unchanged."
)


class OnPremRedactionAdapter:
    """Placeholder PII-redaction adapter for the on-prem profile."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def redact(self, text: str) -> RedactionResult:
        raise NotImplementedError(_MESSAGE)
