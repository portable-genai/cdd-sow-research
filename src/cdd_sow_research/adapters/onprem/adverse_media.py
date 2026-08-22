"""On-prem placeholder for ``AdverseMediaPort`` — the sovereign target.

One of the reversibility (P-02, P-12) migration placeholders. The natural on-premise
posture is a **closed, air-gapped perimeter** with no public-web egress, so this
placeholder does not raise: the dossier pipeline must still complete in a sealed
deployment.

What it does instead is report **not searched** (``None``) rather than an empty result. A
sealed perimeter cannot perform an adverse-media screen, and a dossier that renders that as
"No adverse media found." states a clean screen the deployment never ran. Degrading means
serving less, never serving less-verified. A real on-prem backend (an internal news or
sanctions index) can be filled in later without touching domain logic.
"""

from __future__ import annotations

from ...config import Settings
from ...domain.models import AdverseMediaScreening


class OnPremAdverseMediaAdapter:
    """Placeholder adverse-media adapter for the on-prem profile.

    Defaults model an air-gapped perimeter: no public-web egress, so no screen is
    performed and the case says so rather than claiming a clear result.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def search(self, subject_name: str, max_results: int = 10) -> AdverseMediaScreening | None:
        # Air-gapped: no egress, so no search ran. Not the same as "nothing found".
        return None
