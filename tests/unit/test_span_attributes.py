"""Span ATTRIBUTES carry structure, never content, and this is the test that can tell.

The conftest ``FakeTracer`` records span NAMES (``self.spans.append(name)``), which is right
for the tests that assert the pipeline opened its spans, and structurally blind to the one
defect that matters here: it throws the attributes away, so a span that started carrying the
subject's name or the case text would keep every existing test green. A trace backend is not
the WORM audit trail. It has no redaction stage, a wider read audience and no retention rule
written against a regulator's requirement, so an attribute is OUTSIDE the boundary that
redact-before-everything (R1/P-04) holds.

The recording tracer here keeps ``dict(attributes)``, and the content case drives the
pipeline with ``PII_CASE_INPUT``, whose subject name embeds a planted NRIC and email, so a
leak fails on the planted literal rather than on a subtlety.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from tests.fixtures import sample_cases

ACTOR = "analyst@bank.example"

#: The complete attribute key set a Doc1 span may carry, per span name. Widening one of
#: these is a decision about what leaves the trust boundary, so it is made here rather than
#: at a call site.
_ALLOWED = {
    "cdd.assess": {"action", "actor"},
    "cdd.screen": {"action", "actor"},
}


class _AttributeRecordingTracer:
    """Keeps (name, attributes) per span, unlike the name-only conftest recorder."""

    def __init__(self) -> None:
        self.spans: list[tuple[str, dict[str, str]]] = []

    @contextmanager
    def span(self, name: str, **attributes: str):  # type: ignore[no-untyped-def]
        self.spans.append((name, dict(attributes)))
        yield

    def record_token_usage(self, usage, model):  # type: ignore[no-untyped-def]
        return None


@pytest.fixture
def tracer() -> _AttributeRecordingTracer:  # type: ignore[override]
    """Override the conftest tracer so ``cdd_service`` assembles with THIS recorder."""
    return _AttributeRecordingTracer()


def test_an_assessment_opens_the_named_spans_with_allowlisted_keys_only(
    cdd_service, tracer
) -> None:
    cdd_service.assess(sample_cases.SAMPLE_CASE_INPUT, actor=ACTOR)
    assert [name for name, _ in tracer.spans], "the pipeline opened no span at all"
    for name, attributes in tracer.spans:
        assert name in _ALLOWED, f"unexpected span {name!r}; add it here deliberately"
        assert set(attributes) == _ALLOWED[name], (
            f"span {name!r} attribute keys changed; widening the set is a trust-boundary "
            "decision, so update _ALLOWED here deliberately"
        )


def test_no_span_attribute_carries_the_planted_identifiers(cdd_service, tracer) -> None:
    """PII_CASE_INPUT's subject embeds an NRIC and an email; neither may reach a span."""
    cdd_service.assess(sample_cases.PII_CASE_INPUT, actor=ACTOR)
    emitted = " ".join(value for _, attributes in tracer.spans for value in attributes.values())
    assert "S1234567A" not in emitted
    assert "casey.lim@example.com" not in emitted
    assert "Casey Lim" not in emitted, "the subject's name reached a span attribute"


def test_every_attribute_value_is_a_string(cdd_service, tracer) -> None:
    """The port declares str values; a structured object smuggles content past a grep."""
    cdd_service.assess(sample_cases.SAMPLE_CASE_INPUT, actor=ACTOR)
    for name, attributes in tracer.spans:
        for key, value in attributes.items():
            assert isinstance(value, str), f"span {name!r} attribute {key!r} is not a str"
