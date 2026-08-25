"""The thinking parameter must follow the model that is actually being called.

``config/settings.yaml`` states the principle plainly: model ids are env-overridable "because
model availability is regional: a model that is GA in one region 404s in another, and a
deployment must be able to pin the id its own region actually serves without a fork". The
adapter then pinned Gemini 3's discrete ``thinking_level`` unconditionally, so a deployment
could pin an id its region serves and still not run: a 2.5 model refuses the request outright
with "Unable to submit request because thinking_level is not supported by this model".

The two forms are not interchangeable. Gemini 3 takes a level; 2.5 takes an integer budget.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from cdd_sow_research.adapters.gcp.gemini_llm import _thinking_config
from cdd_sow_research.domain.kernel import ThinkingLevel


class _Level:
    LOW = "LOW"
    HIGH = "HIGH"


class _Types:
    """Just enough of the SDK surface to record which form was constructed."""

    ThinkingLevel = _Level

    @staticmethod
    def ThinkingConfig(**kwargs: object) -> SimpleNamespace:  # noqa: N802 - mirrors the SDK
        return SimpleNamespace(**kwargs)


@pytest.mark.parametrize("model", ["gemini-3.5-flash", "gemini-3.1-flash-lite", "GEMINI-3.1-PRO"])
def test_a_gemini_3_model_gets_the_discrete_level(model: str) -> None:
    config = _thinking_config(model, ThinkingLevel.HIGH, _Types)
    assert config.thinking_level == _Level.HIGH
    assert not hasattr(config, "thinking_budget")


@pytest.mark.parametrize("model", ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.0-flash-001"])
def test_an_earlier_model_gets_a_budget_and_never_a_level(model: str) -> None:
    config = _thinking_config(model, ThinkingLevel.HIGH, _Types)
    assert config.thinking_budget == -1
    assert not hasattr(config, "thinking_level"), (
        "sending thinking_level to a model that does not take it is refused outright, so the "
        "whole request fails rather than degrading"
    )


def test_minimal_asks_for_as_little_thinking_as_the_model_allows() -> None:
    assert _thinking_config("gemini-2.5-flash", ThinkingLevel.MINIMAL, _Types).thinking_budget == 0
    assert (
        _thinking_config("gemini-3.5-flash", ThinkingLevel.MINIMAL, _Types).thinking_level
        == _Level.LOW
    )
