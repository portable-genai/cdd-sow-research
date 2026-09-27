"""The Model Armor adapter allows ONLY a complete, clean screen; everything else blocks.

The mapping this replaced was ``allowed = match_name != "MATCH_FOUND"``: it blocked a match
and allowed everything else. A missing ``sanitization_result``,
``FILTER_MATCH_STATE_UNSPECIFIED``, and ``NO_MATCH_FOUND`` from a screen whose
``invocation_result`` was ``PARTIAL`` or ``FAILURE`` all passed. The last is the dangerous
one: a skipped filter reports ``NO_MATCH_FOUND``, so padding a prompt past the
prompt-injection filter's token limit got it through unscreened. Only ``NO_MATCH_FOUND``
with ``SUCCESS`` allows now, and every sanitize call carries a deadline.

``FilterMatchState`` is a proto-plus ``IntEnum``: on Python 3.11+
``str(FilterMatchState.MATCH_FOUND)`` is ``'2'``, so the mapping must read ``.name``, and a
stub that carried the state as a plain string would hide a mapping that did not.

This module tests at two levels:

* **SDK-free** (always runs, including the SDK-free local profile's ``make check``): the
  mapping is fed ``_MirrorState`` / ``_MirrorInvocation``, stdlib ``IntEnum``s with the real
  member names and numbers, so they have the same ``str()`` behaviour as the real enums.
* **Real SDK** (runs where ``google-cloud-modelarmor`` is installed, as in the CI gate, which
  installs ``requirements-gcp.lock``; skips on the SDK-free local profile): responses are
  built from the real ``modelarmor_v1`` types and screened through ``screen()`` with a fake
  client, so nothing touches the network. The first of these tests also pins the mirrors to
  the real enums, so the SDK-free half cannot drift.
"""

from __future__ import annotations

import enum
from types import SimpleNamespace
from typing import Any

import pytest

from cdd_sow_research.adapters.gcp.model_armor_guardrail import ModelArmorGuardrailAdapter
from cdd_sow_research.config import Settings
from cdd_sow_research.domain.models import Direction

TEXT = "Run CDD on Danube Shipping Ltd and list its ultimate beneficial owners."
DIRECTIONS = [Direction.INPUT, Direction.OUTPUT]


class _MirrorState(enum.IntEnum):
    """``modelarmor_v1.FilterMatchState``'s members, by name and number."""

    FILTER_MATCH_STATE_UNSPECIFIED = 0
    NO_MATCH_FOUND = 1
    MATCH_FOUND = 2


class _MirrorInvocation(enum.IntEnum):
    """``modelarmor_v1.InvocationResult``'s members, by name and number."""

    INVOCATION_RESULT_UNSPECIFIED = 0
    SUCCESS = 1
    PARTIAL = 2
    FAILURE = 3


def _map(response: Any, direction: Direction = Direction.INPUT) -> Any:
    return ModelArmorGuardrailAdapter._to_verdict(response, direction, TEXT)


def _mirror_response(
    state: _MirrorState, invocation: _MirrorInvocation | None = _MirrorInvocation.SUCCESS
) -> SimpleNamespace:
    return SimpleNamespace(
        sanitization_result=SimpleNamespace(filter_match_state=state, invocation_result=invocation)
    )


# --------------------------------------------------------------------------- #
# SDK-free: the mapping itself
# --------------------------------------------------------------------------- #
def test_the_enum_str_is_the_number_not_the_name() -> None:
    """The premise of the fix: a ``str()`` substring check can never see MATCH_FOUND."""
    assert "MATCH_FOUND" not in str(_MirrorState.MATCH_FOUND)


@pytest.mark.parametrize("direction", DIRECTIONS)
def test_match_found_blocks_sdk_free(direction: Direction) -> None:
    verdict = _map(_mirror_response(_MirrorState.MATCH_FOUND), direction)
    assert verdict.allowed is False
    assert verdict.sanitized_text is None
    assert verdict.findings and verdict.findings[0].detail == "Model Armor filter match"


@pytest.mark.parametrize("direction", DIRECTIONS)
def test_no_match_found_allows_sdk_free(direction: Direction) -> None:
    verdict = _map(_mirror_response(_MirrorState.NO_MATCH_FOUND), direction)
    assert verdict.allowed is True
    assert verdict.sanitized_text == TEXT
    assert verdict.findings == ()


@pytest.mark.parametrize("direction", DIRECTIONS)
@pytest.mark.parametrize("invocation", list(_MirrorInvocation), ids=lambda m: m.name)
def test_match_found_blocks_however_many_filters_ran_sdk_free(
    direction: Direction, invocation: _MirrorInvocation
) -> None:
    verdict = _map(_mirror_response(_MirrorState.MATCH_FOUND, invocation), direction)
    assert verdict.allowed is False
    assert verdict.sanitized_text is None


@pytest.mark.parametrize("direction", DIRECTIONS)
@pytest.mark.parametrize(
    "invocation",
    [
        _MirrorInvocation.PARTIAL,
        _MirrorInvocation.FAILURE,
        _MirrorInvocation.INVOCATION_RESULT_UNSPECIFIED,
        None,
    ],
    ids=["PARTIAL", "FAILURE", "UNSPECIFIED", "absent"],
)
def test_no_match_from_an_incomplete_screen_blocks_sdk_free(
    direction: Direction, invocation: _MirrorInvocation | None
) -> None:
    """A skipped filter reports no match. That is not a pass: the text was not screened."""
    verdict = _map(_mirror_response(_MirrorState.NO_MATCH_FOUND, invocation), direction)
    assert verdict.allowed is False
    assert verdict.sanitized_text is None
    assert "no complete filter decision" in verdict.reason


def test_exactly_one_combination_allows_sdk_free() -> None:
    allowed = [
        (state.name, invocation.name)
        for state in _MirrorState
        for invocation in _MirrorInvocation
        if _map(_mirror_response(state, invocation)).allowed
    ]
    assert allowed == [("NO_MATCH_FOUND", "SUCCESS")]


@pytest.mark.parametrize(
    "response",
    [
        _mirror_response(_MirrorState.FILTER_MATCH_STATE_UNSPECIFIED),
        SimpleNamespace(sanitization_result=None),
        SimpleNamespace(sanitization_result=SimpleNamespace()),
        object(),
        None,
    ],
    ids=["unspecified-state", "none-result", "empty-result", "no-result-attr", "none-response"],
)
def test_no_verdict_fails_closed_sdk_free(response: Any) -> None:
    verdict = _map(response)
    assert verdict.allowed is False
    assert verdict.sanitized_text is None


# --------------------------------------------------------------------------- #
# Real SDK: real modelarmor_v1 messages through screen(), with a fake client
# --------------------------------------------------------------------------- #
class _FakeClient:
    """Returns a canned response for either direction, or raises the canned error."""

    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self._response = response
        self._error = error
        self.requests: list[Any] = []
        self.timeouts: list[Any] = []

    def _answer(self, request: Any, timeout: Any) -> Any:
        self.requests.append(request)
        self.timeouts.append(timeout)
        if self._error is not None:
            raise self._error
        return self._response

    def sanitize_user_prompt(self, *, request: Any, timeout: Any = None) -> Any:
        return self._answer(request, timeout)

    def sanitize_model_response(self, *, request: Any, timeout: Any = None) -> Any:
        return self._answer(request, timeout)


def _ma() -> Any:
    return pytest.importorskip("google.cloud.modelarmor_v1")


def _adapter(client: _FakeClient) -> ModelArmorGuardrailAdapter:
    adapter = ModelArmorGuardrailAdapter(Settings(project_id="p", profile="gcp"))
    adapter._client = client  # skip the real client; the mapping is what is under test
    return adapter


def _real_response(
    direction: Direction,
    state_name: str | None,
    invocation_name: str = "SUCCESS",
    *,
    skipped: bool = False,
) -> Any:
    """A real sanitize response; ``state_name=None`` leaves ``sanitization_result`` unset.

    ``skipped`` adds the prompt-injection filter as not having run, the shape a prompt padded
    past that filter's token limit produces.
    """
    ma = _ma()
    cls = (
        ma.SanitizeUserPromptResponse
        if direction is Direction.INPUT
        else ma.SanitizeModelResponseResponse
    )
    if state_name is None:
        return cls()
    filter_results = {}
    if skipped:
        filter_results["pi_and_jailbreak"] = ma.FilterResult(
            pi_and_jailbreak_filter_result=ma.PiAndJailbreakFilterResult(
                execution_state=ma.FilterExecutionState.EXECUTION_SKIPPED,
                match_state=ma.FilterMatchState.NO_MATCH_FOUND,
            )
        )
    return cls(
        sanitization_result=ma.SanitizationResult(
            filter_match_state=ma.FilterMatchState[state_name],
            invocation_result=ma.InvocationResult[invocation_name],
            filter_results=filter_results,
        )
    )


@pytest.mark.parametrize(
    ("mirror", "real_name"),
    [(_MirrorState, "FilterMatchState"), (_MirrorInvocation, "InvocationResult")],
    ids=["FilterMatchState", "InvocationResult"],
)
def test_the_mirror_matches_the_real_enum(mirror: Any, real_name: str) -> None:
    real = getattr(_ma(), real_name)
    assert {m.name: int(m) for m in real} == {m.name: int(m) for m in mirror}
    assert "MATCH_FOUND" not in str(_ma().FilterMatchState.MATCH_FOUND)


@pytest.mark.parametrize("direction", DIRECTIONS)
def test_match_found_blocks(direction: Direction) -> None:
    client = _FakeClient(_real_response(direction, "MATCH_FOUND"))
    verdict = _adapter(client).screen(TEXT, direction)
    assert verdict.allowed is False
    assert verdict.sanitized_text is None
    assert len(client.requests) == 1


@pytest.mark.parametrize("direction", DIRECTIONS)
def test_no_match_found_allows(direction: Direction) -> None:
    client = _FakeClient(_real_response(direction, "NO_MATCH_FOUND"))
    verdict = _adapter(client).screen(TEXT, direction)
    assert verdict.allowed is True
    assert verdict.sanitized_text == TEXT


@pytest.mark.parametrize("direction", DIRECTIONS)
@pytest.mark.parametrize(
    "state_name",
    [None, "FILTER_MATCH_STATE_UNSPECIFIED"],
    ids=["missing-result", "unspecified-state"],
)
def test_no_verdict_fails_closed(direction: Direction, state_name: str | None) -> None:
    client = _FakeClient(_real_response(direction, state_name))
    verdict = _adapter(client).screen(TEXT, direction)
    assert verdict.allowed is False
    assert verdict.sanitized_text is None


@pytest.mark.parametrize("direction", DIRECTIONS)
@pytest.mark.parametrize("invocation_name", ["PARTIAL", "FAILURE", "INVOCATION_RESULT_UNSPECIFIED"])
def test_no_match_from_a_screen_where_filters_did_not_run_blocks(
    direction: Direction, invocation_name: str
) -> None:
    client = _FakeClient(_real_response(direction, "NO_MATCH_FOUND", invocation_name, skipped=True))
    verdict = _adapter(client).screen(TEXT, direction)
    assert verdict.allowed is False
    assert verdict.sanitized_text is None
    assert "no complete filter decision" in verdict.reason


@pytest.mark.parametrize("direction", DIRECTIONS)
def test_every_call_carries_the_deadline(direction: Direction) -> None:
    client = _FakeClient(_real_response(direction, "NO_MATCH_FOUND"))
    _adapter(client).screen(TEXT, direction)
    assert client.timeouts == [Settings().model_armor.timeout_seconds]
    assert client.timeouts[0] > 0


def test_api_errors_propagate() -> None:
    """An API failure must not turn into an allow; it reaches the caller."""
    _ma()
    boom = RuntimeError("Model Armor unavailable")
    with pytest.raises(RuntimeError, match="unavailable"):
        _adapter(_FakeClient(error=boom)).screen(TEXT, Direction.INPUT)
