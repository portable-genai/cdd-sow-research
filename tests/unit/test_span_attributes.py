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

#: The complete attribute key set a cdd-sow-research span may carry, per span name. Widening one of
#: these is a decision about what leaves the trust boundary, so it is made here rather than
#: at a call site.
_ALLOWED = {
    "cdd.assess": {"action", "actor"},
    "cdd.screen": {"action", "actor"},
    # The per-segment child spans. `cdd.assess` alone said a build was slow and nothing about
    # where: the deployment takes ~4-5 minutes where the laptop takes ~50 seconds, and one span
    # cannot be subdivided after the fact.
    "cdd.extract_and_ingest": {"action", "actor", "documents"},
    "cdd.retrieve": {"action", "actor"},
    "cdd.adverse_media": {"action", "actor"},
    "cdd.ownership": {"action", "actor"},
    "cdd.screening": {"action", "actor"},
    "cdd.source_of_wealth": {"action", "actor", "passages"},
    "cdd.risk_rating": {"action", "actor"},
    "cdd.compliance_check": {"action", "actor"},
    "cdd.route_review": {"action", "actor"},
}

#: The segments a completed assessment must have timed. Named here rather than derived from
#: `_ALLOWED` on purpose: `_ALLOWED` is a CEILING on what may be emitted and would still be
#: satisfied by emitting nothing, so a separate floor is what catches instrumentation quietly
#: disappearing. `cdd.route_review` is absent because routing is conditional on a bound router.
_REQUIRED_SEGMENTS = frozenset(
    {
        "cdd.assess",
        "cdd.extract_and_ingest",
        "cdd.retrieve",
        "cdd.adverse_media",
        "cdd.ownership",
        "cdd.screening",
        "cdd.source_of_wealth",
        "cdd.risk_rating",
        "cdd.compliance_check",
    }
)


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


def test_every_consequential_segment_is_timed_separately(cdd_service, tracer) -> None:
    """The floor, not the ceiling: an unattributed pipeline is the defect this closes.

    The deployed dossier build takes ~4-5 minutes where the laptop takes ~50 seconds, and that
    gap survived two sessions unexplained because the only span covered the whole request.
    Screening was ruled out by a hand measurement of one case; nothing else was measured at all.

    This asserts each segment opens its own span, so the next deployed run yields a profile
    rather than another guess. It fails if instrumentation is removed, which the allowlist above
    cannot do: an allowlist is satisfied by emitting nothing.
    """
    cdd_service.assess(sample_cases.SAMPLE_CASE_INPUT, actor=ACTOR)
    opened = {name for name, _ in tracer.spans}
    missing = _REQUIRED_SEGMENTS - opened
    assert not missing, f"these pipeline segments are not timed: {sorted(missing)}"
