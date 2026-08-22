"""Local compliance-client adapter (ComplianceClientPort) — in-process C1 stand-in.

The ``local`` profile's stand-in for the **C1 Compliance Assistant**: under ``local`` this
platform client answers in-process rather than calling C1 over HTTP (a laptop runs one
app, not the whole platform). It returns a deterministic, conservative regulatory CDD/AML
answer that always requires human review, so the dossier's compliance check runs offline
without inventing a clean bill of health. SDK-free and unconditional.
"""

from __future__ import annotations

from ...config import Settings
from ...ports.compliance import ComplianceAnswer


class LocalComplianceClientAdapter:
    """Answer a regulatory CDD/AML question in-process (no HTTP to C1)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def check(self, question: str, actor: str) -> ComplianceAnswer:
        return ComplianceAnswer(
            question=question,
            answer=(
                "Standard CDD and ongoing-monitoring expectations apply (MAS / HKMA / APRA "
                "/ FSA). Verify source of wealth and beneficial ownership, screen for "
                "sanctions and PEP exposure, and route the dossier to a human checker. "
                "This is an offline local-profile answer, not a substitute for the C1 "
                "grounded compliance assistant."
            ),
            citations=(),
            requires_human_review=True,
            confidence=0.7,
        )
