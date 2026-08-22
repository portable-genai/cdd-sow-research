"""Safety ports — the A1 Guardrail Gateway concerns, expressed as interfaces (R1).

B1 handles customer PII (KYC), so the full A1 guardrail + DLP-redaction pipeline is
mandatory (rule R1): redact then guardrail(INPUT), and guardrail(OUTPUT) before the
dossier is returned. Primary GCP adapters: **Model Armor** (prompt-injection /
jailbreak / RAI / malicious URL screening) and **Sensitive Data Protection / DLP**
(``deidentifyContent``) for GA-grade PII redaction before any model call or audit
write (P-04, minimise data to the model).

Each port ships two interchangeable adapters: a direct-GCP adapter (standalone) and a
``platform`` client delegating to the ``agent-guardrail-gateway`` service.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..domain.models import Direction, GuardrailVerdict, RedactionResult


@runtime_checkable
class GuardrailPort(Protocol):
    def screen(self, text: str, direction: Direction) -> GuardrailVerdict:
        """Screen inbound prompt or outbound response; may sanitise in place."""
        ...


@runtime_checkable
class PIIRedactionPort(Protocol):
    def redact(self, text: str) -> RedactionResult:
        """De-identify PII so the result is safe to send to a model or audit sink."""
        ...
