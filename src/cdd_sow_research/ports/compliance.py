"""ComplianceClientPort — the C1 Compliance Assistant dependency.

B1 checks its source-of-wealth / CDD reasoning against regulatory CDD/AML
expectations (MAS / HKMA / APRA / FSA) by asking **C1** (the grounded compliance
assistant). The ``platform`` adapter is a thin HTTP client to C1's ``/ask`` endpoint
(env ``RSK_COMPLIANCE_URL``); the on-prem placeholder stub raises.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from ..domain.models import Citation


@dataclass(frozen=True, slots=True)
class ComplianceAnswer:
    """A C1 answer projected onto the fields B1 consumes (SPEC §6, C1 contract)."""

    question: str
    answer: str
    citations: tuple[Citation, ...] = field(default_factory=tuple)
    requires_human_review: bool = True
    confidence: float = 0.0


@runtime_checkable
class ComplianceClientPort(Protocol):
    def check(self, question: str, actor: str) -> ComplianceAnswer:
        """Ask C1 a regulatory CDD/AML question and return its cited answer."""
        ...
