"""An adverse-media search that never ran must not read as a clean screen.

The dossier console renders ``adverse_media`` and, when it was empty, printed "No adverse
media found." Both SDK-free adapters return no findings by design (the laptop has no
public-web grounding backend, the on-prem target is a sealed perimeter), so every offline
dossier asserted a clean adverse-media screen for a search that never happened. The service
made it worse: it caught every adapter exception and returned an empty tuple, so an adapter
that correctly refused was flattened into the same affirmative.

The repository already models the correct shape one field above, for watchlist screening:
``None`` means not screened, an empty ``alerts`` means screened and clear. This pins the
same distinction for adverse media, at the four layers it has to survive: the service, the
two offline adapters, the assembled case, and the wire.

Every test here was RED before the fix.
"""

from __future__ import annotations

from cdd_sow_research.adapters.local.adverse_media import LocalCannedAdverseMediaAdapter
from cdd_sow_research.adapters.onprem.adverse_media import OnPremAdverseMediaAdapter
from cdd_sow_research.config import Settings
from cdd_sow_research.domain.adverse_media_service import AdverseMediaService
from cdd_sow_research.domain.models import (
    AdverseMediaFinding,
    AdverseMediaScreening,
    Severity,
    Subject,
)
from cdd_sow_research.domain.services import AdverseMediaService as ExportedService

_SUBJECT = Subject(id="subj-1", name="Fictional Holdings Pte Ltd", jurisdiction="SG")


class _NotSearched:
    """A port with no reachable source: it says so instead of returning nothing."""

    def search(self, subject_name: str, max_results: int = 10) -> AdverseMediaScreening | None:
        return None


class _Refuses:
    """A port that raises. Best-effort must not mean best-effort-then-claim-clear."""

    def search(self, subject_name: str, max_results: int = 10) -> AdverseMediaScreening | None:
        raise RuntimeError("adverse-media backend unreachable")


class _SearchedAndClear:
    def search(self, subject_name: str, max_results: int = 10) -> AdverseMediaScreening | None:
        return AdverseMediaScreening(subject_name=subject_name, sources=("news-index",))


class _SearchedAndHit:
    def search(self, subject_name: str, max_results: int = 10) -> AdverseMediaScreening | None:
        return AdverseMediaScreening(
            subject_name=subject_name,
            findings=(
                AdverseMediaFinding(
                    headline="Regulator opens review into Fictional Holdings",
                    publisher="Fictional Wire",
                    url="https://example.invalid/a",
                    severity=Severity.LOW,
                ),
                AdverseMediaFinding(
                    headline="Enforcement action reported against Fictional Holdings",
                    publisher="Fictional Wire",
                    url="https://example.invalid/b",
                    severity=Severity.CRITICAL,
                ),
            ),
            sources=("news-index",),
        )


def _service(port: object) -> AdverseMediaService:
    return AdverseMediaService(adverse_media=port, tracer=None)


def test_a_source_that_was_never_searched_is_not_a_clear_screen() -> None:
    """The defect: no reachable source returning (), which renders as "none found"."""
    assert _service(_NotSearched()).scan(_SUBJECT, "tester") is None


def test_an_adapter_that_raises_is_not_a_clear_screen() -> None:
    """The same defect one layer up: a blanket except returns () and hides the refusal."""
    assert _service(_Refuses()).scan(_SUBJECT, "tester") is None


def test_a_search_that_ran_and_found_nothing_IS_a_clear_screen() -> None:
    """The other side of the distinction: this one may legitimately render "none found"."""
    screening = _service(_SearchedAndClear()).scan(_SUBJECT, "tester")
    assert screening is not None
    assert screening.findings == ()
    assert screening.sources == ("news-index",)


def test_findings_still_arrive_most_severe_first_with_citations() -> None:
    screening = _service(_SearchedAndHit()).scan(_SUBJECT, "tester")
    assert screening is not None
    assert [f.severity for f in screening.findings] == [Severity.CRITICAL, Severity.LOW]
    assert all(f.citation is not None for f in screening.findings)


def test_the_exported_service_is_the_same_object() -> None:
    """domain.services re-exports rather than redeclaring."""
    assert ExportedService is AdverseMediaService


def test_the_local_adapter_reports_not_searched_rather_than_clear() -> None:
    """The laptop has no grounding backend, which is not the same as clean."""
    adapter = LocalCannedAdverseMediaAdapter(Settings())
    assert adapter.enabled is False
    assert adapter.search("Fictional Holdings Pte Ltd") is None


def test_the_onprem_adapter_reports_not_searched_rather_than_clear() -> None:
    """A sealed perimeter has no public-web egress, which is not the same as clean."""
    adapter = OnPremAdverseMediaAdapter(Settings())
    assert adapter.search("Fictional Holdings Pte Ltd") is None
