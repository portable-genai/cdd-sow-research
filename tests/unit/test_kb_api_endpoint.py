"""Which Discovery Engine host each Agent Search location is reached through.

Agent Search has exactly three locations -- ``global``, ``us`` and ``eu`` -- and only the two
jurisdictional ones carry a location prefix in the hostname. ``global`` is served by the bare
host, so prefixing unconditionally produced ``global-discoveryengine.googleapis.com``, which
does not resolve. The failure was not loud: ingestion targeted a dead hostname while the
upload reported the case-store write, so a document uploaded, listed and then grounded
nothing, and the dossier was refused for want of evidence the operator had just supplied.

Location is a deployment input, not a constant: an asia-southeast1 deployment must select
``global`` because Agent Search serves no APAC region at all.
"""

from __future__ import annotations

import pytest

from cdd_sow_research.adapters.gcp.agent_search_kb import _api_endpoint


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("global", "discoveryengine.googleapis.com"),
        ("us", "us-discoveryengine.googleapis.com"),
        ("eu", "eu-discoveryengine.googleapis.com"),
    ],
)
def test_endpoint_per_location(location: str, expected: str) -> None:
    assert _api_endpoint(location) == expected


def test_global_is_never_prefixed() -> None:
    """The specific string that made the default location unaddressable."""
    assert _api_endpoint("global") != "global-discoveryengine.googleapis.com"
