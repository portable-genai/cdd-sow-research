"""On-prem placeholder for ``GuardrailPort`` — the sovereign target.

One of the reversibility (P-02, P-12) migration placeholders: in the managed profile
this port binds to the Model Armor adapter; switching ``profile`` to ``onprem`` rebinds
it here. The adapter constructs cleanly with **no external dependencies** and
structurally satisfies the same Protocol as the managed adapter, so the contract tests
prove interface parity. ``screen`` deliberately raises rather than fail-opening: an
unimplemented guardrail must never silently allow traffic, so porting on-premise *must*
provide a real screening backend (rule R1). Filling this body in is the only change
required.
"""

from __future__ import annotations

from ...config import Settings
from ...domain.models import Direction, GuardrailVerdict

_MESSAGE = (
    "On-prem GuardrailPort adapter is a migration placeholder; implement against your "
    "on-premise platform. Core domain logic is unchanged."
)


class OnPremGuardrailAdapter:
    """Placeholder guardrail adapter for the on-prem profile."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def screen(self, text: str, direction: Direction) -> GuardrailVerdict:
        raise NotImplementedError(_MESSAGE)
