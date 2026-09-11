"""ComplianceClientPort: the `compliance-advisory` dependency.

`cdd-sow-research` checks each dossier's rating against regulatory CDD/AML expectations by asking
`compliance-advisory`, the grounded compliance assistant. Every networked profile binds the thin
HTTP client to its ``/ask`` endpoint (``adapters/platform/remote_compliance.py``, env
``RSK_COMPLIANCE_URL``); the offline gate answers in-process; the on-prem placeholder raises.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..domain.models import ComplianceAnswer


@runtime_checkable
class ComplianceClientPort(Protocol):
    def check(self, question: str, actor: str) -> ComplianceAnswer:
        """Ask compliance-advisory a regulatory CDD/AML question and return its cited answer."""
        ...
