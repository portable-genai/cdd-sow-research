"""How many extractive segments the managed knowledge base may be asked for.

``max_extractive_segment_count`` was ``max(query.top_k, 1)``, passed through unbounded. Discovery
Engine refuses anything above ten with ``400 max_extractive_segment_count must be between 0 and 10
(inclusive), but is set to 20``, so a retrieval tuned wider than the API ceiling did not degrade:
it failed the entire search. Empty retrieval is a hard error in this system rather than an
ungrounded answer, so the request produced no dossier at all.

Found by running ``make test-managed`` against the named deployment on 2026-08-26, which is the
kind of defect no offline profile reaches: the local index has no such ceiling and every unit
test passed while the managed path could not search at all.
"""

from __future__ import annotations

import pytest

from cdd_sow_research.adapters.gcp.agent_search_kb import (
    _MAX_EXTRACTIVE_SEGMENTS,
    _extractive_segment_count,
)


def test_the_api_ceiling_is_the_one_the_service_enforces() -> None:
    """Ten is the documented limit. A higher constant would restore the 400."""

    assert _MAX_EXTRACTIVE_SEGMENTS == 10


@pytest.mark.parametrize("top_k", [11, 20, 50, 1000])
def test_a_wider_top_k_is_clamped_rather_than_refused(top_k: int) -> None:
    """The caller keeps its own top_k for ranking; the API is asked only for what it accepts."""

    assert _extractive_segment_count(top_k) == _MAX_EXTRACTIVE_SEGMENTS


@pytest.mark.parametrize(("top_k", "expected"), [(1, 1), (5, 5), (10, 10)])
def test_a_top_k_within_the_ceiling_is_passed_through(top_k: int, expected: int) -> None:
    assert _extractive_segment_count(top_k) == expected


@pytest.mark.parametrize("top_k", [0, -1, -100])
def test_a_non_positive_top_k_still_asks_for_one_segment(top_k: int) -> None:
    """Zero segments would return no extractive content and read as an empty index."""

    assert _extractive_segment_count(top_k) == 1


def test_the_shipped_top_k_default_would_not_have_tripped_the_ceiling() -> None:
    """Why this survived so long, pinned so the reason cannot quietly change.

    ``config/settings.yaml`` ships ``top_k: 10``, exactly the ceiling, so the default
    configuration was the one value that could not fail. Only a deployment tuned wider hit the
    400. If the shipped default ever exceeds the ceiling, the unclamped call would have failed
    for everyone and this test says so at the point the default moves.
    """

    from cdd_sow_research.config import Settings

    settings = Settings.load()
    assert settings.knowledge_base.top_k <= _MAX_EXTRACTIVE_SEGMENTS
