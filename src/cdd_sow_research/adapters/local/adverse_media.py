"""Local adverse-media adapter (AdverseMediaPort) — no grounding backend, so no search.

The ``local`` profile's stand-in for the Gemini ``google_search`` grounding sub-agent: a
laptop run has no public-web grounding backend, so web egress is OFF (``enabled`` is
``False``) and no search happens at all.

It therefore reports **not searched** (``None``) rather than an empty result. Those are
different facts. "We looked and the subject is clean" is an assertion about the subject;
"we could not look" is an assertion about this deployment, and only the second one is true
here. Returning an empty list conflated them, and the console printed "No adverse media
found." on every offline dossier: a clean adverse-media screen the system never performed.

There is no Google emulator for web grounding, so this path is unconditional and SDK-free.
"""

from __future__ import annotations

from ...config import Settings
from ...domain.models import AdverseMediaScreening


class LocalCannedAdverseMediaAdapter:
    """Offline adverse-media scan: no public-web egress, so no screen is performed."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def enabled(self) -> bool:
        # No public-web grounding backend on a laptop: web egress is off.
        return False

    def search(self, subject_name: str, max_results: int = 10) -> AdverseMediaScreening | None:
        # No egress means no search ran. Say so; do not report a clean screen.
        return None
