"""Local compliance-client adapter (ComplianceClientPort): the offline gate's in-process answer.

Bound only under ``local``, the SDK-free profile the gate and the tests run on. It answers
in-process rather than calling `compliance-advisory` over HTTP, and it says so in the answer
itself: a deterministic, conservative regulatory CDD/AML statement with no citation that always
requires human review, so the dossier's compliance check runs offline without inventing a clean
bill of health. Every other profile, the deployment and the laptop demo included, asks the real
service through ``adapters/platform/remote_compliance.py``.
"""

from __future__ import annotations

from ...config import Settings
from ...domain.models import ComplianceAnswer


class LocalComplianceClientAdapter:
    """Answer a regulatory CDD/AML question in-process, with no HTTP call."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def check(self, question: str, actor: str) -> ComplianceAnswer:
        return ComplianceAnswer(
            question=question,
            answer=(
                "Standard CDD and ongoing-monitoring expectations apply (MAS / HKMA / APRA "
                "/ FSA). Verify source of wealth and beneficial ownership, screen for "
                "sanctions and PEP exposure, and route the dossier to a human checker. "
                "This is an offline local-profile answer, not a substitute for the grounded "
                "compliance-advisory service."
            ),
            citations=(),
            requires_human_review=True,
            confidence=0.7,
        )
