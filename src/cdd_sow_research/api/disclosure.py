"""Put what the runtime controls did on a response, so the user who asked can see it.

One field here, ``review_routing``: what happened to the human-review hand-off, one of
``routed``, ``failed``, ``off`` or ``not_required``. It comes from the request-scoped
:class:`~cdd_sow_research.adapters.controls.RecordingReviewRouter`, which the route receives
from the same FastAPI dependency its service was built with.

There is no ``input_redacted`` here on purpose. Redaction in this service de-identifies the
case summary that goes to the guardrail and the audit record; the model is given the subject
by name because a CDD dossier is about that subject. Telling the user their input was masked
"before the model saw it" would be false, so nothing is said.
"""

from __future__ import annotations

from pydantic import BaseModel

from ..adapters.controls import RecordingReviewRouter


def disclose[ResponseT: BaseModel](
    response: ResponseT, *, routing: RecordingReviewRouter | None = None
) -> ResponseT:
    """Return ``response`` with the hand-off outcome for this request filled in."""
    if routing is None:
        return response
    return response.model_copy(update={"review_routing": routing.outcome.value})
