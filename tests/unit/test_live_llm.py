"""The live LLM adapter: talking to a local OpenAI-compatible server.

The services ask for structured output, and local servers answer with whatever a chat
model feels like emitting, so the behaviour that matters is: recover the JSON object from
each of the shapes a model actually produces, retry once when there is none, and never
let a triage blip take down an assessment. HTTP is mocked with respx, so no model server
is needed to run these.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from cdd_sow_research.adapters.live._client import LocalModelError
from cdd_sow_research.adapters.live.llm import GemmaLocalLLMAdapter
from cdd_sow_research.config import LiveSettings, Settings
from cdd_sow_research.domain.models import LlmMessage, LlmRequest

_URL = "http://127.0.0.1:8001/chat/completions"
_SCHEMA = {"type": "object", "properties": {"narrative": {"type": "string"}}}


def _adapter(**live: object) -> GemmaLocalLLMAdapter:
    return GemmaLocalLLMAdapter(Settings(profile="live", live=LiveSettings(llm_url=_URL, **live)))


def _request(schema: dict | None = None) -> LlmRequest:
    return LlmRequest(
        messages=(LlmMessage(role="user", content="Evidence: [doc-1 p.2] ..."),),
        system_instruction="You are a CDD analyst.",
        response_schema=schema,
    )


def _reply(content: str, usage: dict | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "gemma-4",
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": usage or {"input_tokens": 120, "output_tokens": 40},
        },
    )


@respx.mock
def test_clean_json_is_returned_as_the_response_text():
    respx.post(_URL).mock(return_value=_reply('{"narrative": "Wealth from logistics."}'))

    response = _adapter().generate(_request(_SCHEMA))

    assert json.loads(response.text)["narrative"] == "Wealth from logistics."
    assert response.usage.input_tokens == 120
    assert response.model == "gemma-4"


@respx.mock
def test_json_wrapped_in_a_code_fence_is_recovered():
    respx.post(_URL).mock(
        return_value=_reply('Here you go:\n```json\n{"narrative": "Fenced."}\n```')
    )

    response = _adapter().generate(_request(_SCHEMA))

    assert json.loads(response.text) == {"narrative": "Fenced."}


@respx.mock
def test_json_surrounded_by_prose_is_recovered():
    respx.post(_URL).mock(return_value=_reply('Sure. {"narrative": "Inline."} Hope that helps.'))

    response = _adapter().generate(_request(_SCHEMA))

    assert json.loads(response.text) == {"narrative": "Inline."}


@respx.mock
def test_an_unparseable_answer_earns_one_stricter_retry():
    route = respx.post(_URL).mock(
        side_effect=[_reply("I cannot do that."), _reply('{"narrative": "Second try."}')]
    )

    response = _adapter().generate(_request(_SCHEMA))

    assert route.call_count == 2
    assert json.loads(response.text) == {"narrative": "Second try."}
    # The retry replays the failed answer and asks again, so the model sees its own miss.
    retry_body = json.loads(route.calls[1].request.content)
    assert retry_body["messages"][-1]["role"] == "user"
    assert "not valid JSON" in retry_body["messages"][-1]["content"]


@respx.mock
def test_a_still_unparseable_answer_degrades_to_raw_text():
    respx.post(_URL).mock(return_value=_reply("still not JSON"))

    response = _adapter().generate(_request(_SCHEMA))

    # The domain's tolerant parser then yields an empty body: a thin dossier, not a crash.
    assert response.text == "still not JSON"


@respx.mock
def test_the_requested_schema_is_stated_in_the_system_prompt():
    route = respx.post(_URL).mock(return_value=_reply('{"narrative": "x"}'))

    _adapter().generate(_request(_SCHEMA))

    body = json.loads(route.calls[0].request.content)
    system = body["messages"][0]
    assert system["role"] == "system"
    assert "You are a CDD analyst." in system["content"]
    assert '"narrative"' in system["content"]


@respx.mock
def test_a_schemaless_request_is_returned_verbatim():
    respx.post(_URL).mock(return_value=_reply("A plain sentence."))

    assert _adapter().generate(_request()).text == "A plain sentence."


@respx.mock
def test_model_roles_are_mapped_to_the_openai_vocabulary():
    route = respx.post(_URL).mock(return_value=_reply("ok"))
    request = LlmRequest(
        messages=(
            LlmMessage(role="user", content="first"),
            LlmMessage(role="model", content="previous answer"),
        )
    )

    _adapter().generate(request)

    roles = [m["role"] for m in json.loads(route.calls[0].request.content)["messages"]]
    assert roles == ["user", "assistant"]


@respx.mock
def test_an_unreachable_server_says_so_and_names_the_setting():
    respx.post(_URL).mock(side_effect=httpx.ConnectError("connection refused"))

    with pytest.raises(LocalModelError) as excinfo:
        _adapter().generate(_request())

    assert "CDD_LIVE_LLM_URL" in str(excinfo.value)


@respx.mock
def test_classify_returns_one_of_the_offered_labels():
    respx.post(_URL).mock(return_value=_reply("The answer is high."))

    assert _adapter().classify("severe fraud reporting", ["low", "high"]) == "high"


@respx.mock
def test_classify_falls_back_rather_than_failing_an_assessment():
    respx.post(_URL).mock(side_effect=httpx.ConnectError("connection refused"))

    # Triage only routes; a blip must not become a failed dossier.
    assert _adapter().classify("anything", ["low", "high"]) == "low"
