"""The runtime-control seam: what a switched-off control binds, and what a caller reports.

Two halves, both profile-independent, so they live beside the adapter families rather than in
one of them.

**Disabled adapters.** When a deployment switches a cheap runtime control off
(``CDD_GUARDRAIL``, ``CDD_PII_REDACTION``, ``CDD_REVIEW_ROUTING``), the container binds one of
these instead of the profile's class. Each satisfies its port and does nothing, so no service
grows a ``None`` branch, and the container logs the posture once at startup.

**Recording wrapper.** Every caller that hands a result to the review router (the API routes,
the agent tools, the CLI and the MCP server) wraps the bound router in
:class:`RecordingReviewRouter` for that one call, so what it returns can say what happened to
the hand-off: ``routed``, ``failed``, ``off`` or ``not_required``. A failure is logged and
absorbed here, because an already-assembled, already-audited dossier must not fail on a
console outage, but it is never invisible: the caller reports ``failed``.

The port's verbs return ``None`` for "accepted". An adapter that did NOT hand the item off,
because routing is switched off or because this wrapper absorbed a failure, returns ``False``,
which is how the perpetual-KYC and UBO services keep their own ``routed_to_hrz7`` flag and
audit field honest without importing anything from here.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from enum import StrEnum
from typing import Any

from ..config import Settings
from ..domain.models import (
    CDDCase,
    Direction,
    GuardrailVerdict,
    PerpetualKycAssessment,
    RedactionResult,
    UboResolution,
)

_log = logging.getLogger(__name__)


class ReviewRouting(StrEnum):
    """What happened to the human-review hand-off for one result."""

    ROUTED = "routed"
    FAILED = "failed"
    OFF = "off"
    NOT_REQUIRED = "not_required"


# --------------------------------------------------------------------------- #
# Disabled adapters
# --------------------------------------------------------------------------- #
class DisabledGuardrail:
    """GuardrailPort with the guardrail switched off: allows everything, text unchanged."""

    enabled = False

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def screen(self, text: str, direction: Direction) -> GuardrailVerdict:
        return GuardrailVerdict(
            allowed=True, direction=direction, sanitized_text=text, reason="guardrail off"
        )


class DisabledRedaction:
    """PIIRedactionPort with redaction switched off: text unchanged, no findings."""

    enabled = False

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def redact(self, text: str) -> RedactionResult:
        return RedactionResult(text=text, findings=())


class DisabledReviewRouter:
    """ReviewRouterPort with routing switched off: nothing is submitted anywhere."""

    enabled = False

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def route(self, case: CDDCase, *, maker: str) -> bool:
        return False

    def route_monitoring(self, assessment: PerpetualKycAssessment, *, maker: str) -> bool:
        return False

    def route_ownership(self, resolution: UboResolution, *, maker: str) -> bool:
        return False


# --------------------------------------------------------------------------- #
# Recording wrapper
# --------------------------------------------------------------------------- #
class RecordingReviewRouter:
    """Wraps the bound review router for one caller and records each hand-off's outcome."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self._outcomes: list[ReviewRouting] = []

    def route(self, case: CDDCase, *, maker: str) -> bool | None:
        return self._hand_off(lambda: self._inner.route(case, maker=maker))

    def route_monitoring(self, assessment: PerpetualKycAssessment, *, maker: str) -> bool | None:
        return self._hand_off(lambda: self._inner.route_monitoring(assessment, maker=maker))

    def route_ownership(self, resolution: UboResolution, *, maker: str) -> bool | None:
        return self._hand_off(lambda: self._inner.route_ownership(resolution, maker=maker))

    def _hand_off(self, submit: Callable[[], object]) -> bool | None:
        if not getattr(self._inner, "enabled", True):
            self._outcomes.append(ReviewRouting.OFF)
            return False
        try:
            submit()
        except Exception as exc:  # noqa: BLE001 - the outcome is reported, never raised
            _log.warning("human-review hand-off failed: %s", type(exc).__name__)
            self._outcomes.append(ReviewRouting.FAILED)
            return False
        self._outcomes.append(ReviewRouting.ROUTED)
        return None

    @property
    def outcome(self) -> ReviewRouting:
        """One value for the caller: any failure wins, then off, then routed."""
        for worst in (ReviewRouting.FAILED, ReviewRouting.OFF, ReviewRouting.ROUTED):
            if worst in self._outcomes:
                return worst
        return ReviewRouting.NOT_REQUIRED
