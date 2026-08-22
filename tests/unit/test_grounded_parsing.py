"""Grounded answers arrive wrapped, and must still be read.

A model using the built-in ``google_search`` tool cannot also be put in JSON mode, so it
answers with a fenced block or a sentence of preamble. Parsing that strictly produced an
empty adverse-media list and an empty owner list, which a reviewer reads as "clean
subject, transparent ownership" rather than as a failure. These pin the tolerant read on
the shapes the models actually return.

Parsing only: no network, no SDK. The adapters' ``_parse`` helpers are static.
"""

from __future__ import annotations

import pytest

from cdd_sow_research.adapters.gcp.gemini_adverse_media import GeminiAdverseMediaAdapter
from cdd_sow_research.adapters.gcp.registry_lookup import GroundedRegistryAdapter
from cdd_sow_research.domain._grounded import parse_json_object

_OWNERS_JSON = """{
  "owners": [
    {"name": "Temasek Holdings", "ownership_percentage": 28.3, "country": "SG", "is_pep": true},
    {"name": "Public Shareholders", "pct": 71.7, "country": "Various", "is_pep": false}
  ],
  "tree": {"name": "DBS Group Holdings Ltd"}
}"""

_MEDIA_JSON = """{
  "findings": [
    {
      "headline": "Regulator fines the group over control failings",
      "publisher": "Example Wire",
      "url": "https://news.example.com/story",
      "category": "money_laundering",
      "severity": "high",
      "snippet": "The regulator cited weak transaction monitoring."
    }
  ]
}"""


def _fenced(payload: str) -> str:
    return f"```json\n{payload}\n```"


def _prefixed(payload: str) -> str:
    return f"Based on my search of public sources, here is the result:\n\n{payload}"


@pytest.mark.parametrize("wrap", [str, _fenced, _prefixed])
def test_owners_are_recovered_however_the_model_wraps_them(wrap):
    summary = GroundedRegistryAdapter._parse("DBS Group Holdings Ltd", wrap(_OWNERS_JSON))

    assert [o.name for o in summary.owners] == ["Temasek Holdings", "Public Shareholders"]
    assert summary.owners[0].is_pep is True


@pytest.mark.parametrize("wrap", [str, _fenced, _prefixed])
def test_adverse_media_is_recovered_however_the_model_wraps_it(wrap):
    findings = GeminiAdverseMediaAdapter._parse(wrap(_MEDIA_JSON))

    assert len(findings) == 1
    assert findings[0].category.value == "money_laundering"
    assert findings[0].severity.value == "high"


def test_an_ownership_stake_is_read_whichever_key_the_model_chose():
    summary = GroundedRegistryAdapter._parse("DBS Group Holdings Ltd", _OWNERS_JSON)

    # A stake silently read as 0% understates control in a UBO summary.
    assert [o.pct for o in summary.owners] == [28.3, 71.7]


def test_the_ownership_tree_reads_the_same_percentage_keys_as_the_owners():
    """The tree must not accept only "pct": a model that says
    "ownership_percentage" would produce a tree of 0% stakes beside correct owner rows."""
    payload = """{
      "owners": [],
      "tree": {
        "name": "Root Holdings Ltd",
        "ownership_percentage": 100,
        "children": [
          {"name": "Midco Pte Ltd", "percentage": 60,
           "children": [{"name": "Opco Ltd", "pct": 45}]}
        ]
      }
    }"""

    tree = GroundedRegistryAdapter._parse("Root Holdings Ltd", payload).tree

    assert tree is not None
    assert tree.pct == 100.0
    assert tree.children[0].pct == 60.0
    assert tree.children[0].children[0].pct == 45.0


def test_a_missing_percentage_is_zero_not_a_crash():
    tree = GroundedRegistryAdapter._parse("Acme Ltd", '{"tree": {"name": "Acme Ltd"}}').tree

    assert tree is not None
    assert tree.pct == 0.0


def test_a_genuinely_empty_result_stays_empty():
    assert GeminiAdverseMediaAdapter._parse('{"findings": []}') == []
    assert GroundedRegistryAdapter._parse("Acme Ltd", '{"owners": []}').owners == ()


@pytest.mark.parametrize("text", ["", "I could not find anything.", "```json\nnot json\n```"])
def test_an_unusable_answer_degrades_rather_than_raising(text: str):
    assert parse_json_object(text) == {}
    assert GeminiAdverseMediaAdapter._parse(text) == []
    assert GroundedRegistryAdapter._parse("Acme Ltd", text).root_entity == "Acme Ltd"


def test_a_json_array_answer_is_wrapped_not_discarded():
    assert parse_json_object('[{"a": 1}]') == {"items": [{"a": 1}]}


def test_hard_reasoning_is_off_by_default_and_selects_the_model_when_switched_on():
    """A settings switch that selects nothing at all is worse than no switch: config
    that reports success while doing nothing."""
    from dataclasses import replace

    from cdd_sow_research.adapters.gcp.gemini_llm import GeminiLLMAdapter
    from cdd_sow_research.config import Settings

    settings = Settings.load()
    default = GeminiLLMAdapter(settings)
    assert default._reasoning_model() == settings.models.reasoning

    opted_in = GeminiLLMAdapter(
        replace(settings, models=replace(settings.models, use_hard_reasoning=True))
    )
    assert opted_in._reasoning_model() == settings.models.hard_reasoning
