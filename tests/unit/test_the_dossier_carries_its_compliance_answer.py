"""The dossier carries the compliance answer it asked for, and says when there is none.

``CddService`` asked `compliance-advisory` a regulatory question on every assessment and then
dropped the answer: ``_compliance_check`` returned nothing, ``CDDCase`` had no field for it and
``CddCaseResponse`` had nothing to project. A capability that is built, bound and called but
never reaches the wire is indistinguishable from one that was never built, so rebinding the
port to the real service would have changed nothing a reviewer, or the paired demonstration,
could see.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging

from fastapi.responses import JSONResponse
from tests.fixtures import sample_cases

from cdd_sow_research.api.app import export_portable_dossier, import_portable_dossier
from cdd_sow_research.api.schemas import CddCaseResponse, PortableDossierArtifact
from cdd_sow_research.domain.identity import Principal
from cdd_sow_research.domain.models import (
    CDDCase,
    Citation,
    ComplianceAnswer,
    Direction,
    SourceType,
)

ACTOR = "analyst@bank.test"
_LOGGER_NAME = "cdd_sow_research.domain.cdd_service"

_REGULATION = Citation(
    source_id="notice-626",
    source_type=SourceType.REGULATION,
    title="Notice on customer due diligence (FICTIONAL)",
    url="https://regulator.example/notice-626",
    page=12,
    snippet="A financial institution shall perform enhanced CDD ...",
    score=0.81,
)


def _principal() -> Principal:
    return Principal(
        subject=ACTOR,
        principals=("group:cdd-analyst",),
        tenant="bank-test",
        assurance="local",
        source="test",
    )


class _Unreachable:
    """A bound compliance client whose service refuses the connection."""

    def check(self, question: str, actor: str) -> ComplianceAnswer:
        raise ConnectionError("compliance-advisory refused the connection")


# --------------------------------------------------------------------------------------- #
# The service keeps what it asked for
# --------------------------------------------------------------------------------------- #
def test_the_answer_reaches_the_dossier(cdd_service, compliance) -> None:
    case = cdd_service.assess(sample_cases.SAMPLE_CASE_INPUT, actor=ACTOR)

    assert compliance.calls, "the dossier must still ask compliance-advisory"
    assert case.compliance is not None, "an answer that was asked for and dropped"
    asked, _ = compliance.calls[-1]
    assert case.compliance.question == asked
    assert case.compliance.requires_human_review is True


def test_the_question_is_built_only_from_compared_fields(cdd_service) -> None:
    """The question is the one part of the exchange the pair can compare exactly, because it is
    a pure function of the subject type, the jurisdiction and the band, all compared already."""
    case = cdd_service.assess(sample_cases.SAMPLE_CASE_INPUT, actor=ACTOR)

    assert case.compliance is not None
    question = case.compliance.question
    assert case.subject.type.value in question
    assert (case.subject.jurisdiction or "an unknown") in question
    assert case.rating.band.value in question
    again = cdd_service.assess(sample_cases.SAMPLE_CASE_INPUT, actor=ACTOR)
    assert again.compliance is not None and again.compliance.question == question


def test_an_unanswered_check_is_null_and_says_why(cdd_service, caplog) -> None:
    """An outage degrades the dossier, never fails it, and never reads as an answer."""
    cdd_service._compliance = _Unreachable()

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        case = cdd_service.assess(sample_cases.SAMPLE_CASE_INPUT, actor=ACTOR)

    assert case.compliance is None
    messages = [record.getMessage() for record in caplog.records]
    assert any("NOT CHECKED" in m and "refused the connection" in m for m in messages), messages


def test_the_answer_passes_the_output_screen(cdd_service, guardrail) -> None:
    """Text another service generated leaves this one inside the dossier, so it is screened
    where it crosses the boundary like the narrative beside it."""
    case = cdd_service.assess(sample_cases.SAMPLE_CASE_INPUT, actor=ACTOR)

    assert case.compliance is not None
    screened = [text for text, direction in guardrail.calls if direction is Direction.OUTPUT]
    assert any(case.compliance.answer in text for text in screened), screened


# --------------------------------------------------------------------------------------- #
# The wire
# --------------------------------------------------------------------------------------- #
def test_every_dossier_field_is_on_the_wire() -> None:
    """Structural rather than a list, because a hand-kept list is what went stale."""
    domain = {field.name for field in dataclasses.fields(CDDCase)}
    wire = set(CddCaseResponse.model_fields)
    assert not domain - wire, f"built but never sent: {sorted(domain - wire)}"


def test_the_wire_projects_the_answer_and_its_citations(cdd_service) -> None:
    case = cdd_service.assess(sample_cases.SAMPLE_CASE_INPUT, actor=ACTOR)
    answered = dataclasses.replace(
        case,
        compliance=ComplianceAnswer(
            question="What CDD expectations apply?",
            answer="Enhanced due diligence applies.",
            citations=(_REGULATION,),
            requires_human_review=True,
            confidence=0.74,
        ),
    )

    wire = CddCaseResponse.from_domain(answered).model_dump(mode="json")["compliance"]

    assert wire["question"] == "What CDD expectations apply?"
    assert wire["requires_human_review"] is True
    assert wire["confidence"] == 0.74
    assert [c["source_type"] for c in wire["citations"]] == ["regulation"]
    assert wire["citations"][0]["title"] == "Notice on customer due diligence (FICTIONAL)"
    assert wire["citations"][0]["page"] == 12


def test_no_answer_is_null_on_the_wire(cdd_service) -> None:
    case = dataclasses.replace(
        cdd_service.assess(sample_cases.SAMPLE_CASE_INPUT, actor=ACTOR), compliance=None
    )
    assert CddCaseResponse.from_domain(case).model_dump(mode="json")["compliance"] is None


# --------------------------------------------------------------------------------------- #
# Portable dossiers exported before the field existed
# --------------------------------------------------------------------------------------- #
def _digest_as_exported_before(payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def test_a_dossier_exported_before_the_field_existed_still_reloads(cdd_service) -> None:
    """An additive field must not turn every earlier export into an integrity failure."""
    case = dataclasses.replace(
        cdd_service.assess(sample_cases.SAMPLE_CASE_INPUT, actor=ACTOR), compliance=None
    )
    payload = CddCaseResponse.from_domain(case).model_dump(mode="json")
    del payload["compliance"]
    artifact = PortableDossierArtifact.model_validate(
        {
            "sha256": _digest_as_exported_before(payload),
            "exported_at": "2026-09-01T00:00:00+00:00",
            "dossier": payload,
        }
    )

    reloaded = import_portable_dossier(artifact, _principal())

    assert isinstance(reloaded, CddCaseResponse), reloaded


def test_a_rewritten_compliance_answer_is_refused(cdd_service) -> None:
    """The answer is inside the digest once it exists, so it cannot be edited in transit."""
    dossier = CddCaseResponse.from_domain(
        cdd_service.assess(sample_cases.SAMPLE_CASE_INPUT, actor=ACTOR)
    )
    assert dossier.compliance is not None
    artifact = export_portable_dossier(dossier, _principal())
    assert isinstance(artifact, PortableDossierArtifact)

    rewritten = dossier.model_copy(
        update={
            "compliance": dossier.compliance.model_copy(
                update={"answer": "No due diligence is required."}
            )
        }
    )
    response = import_portable_dossier(
        artifact.model_copy(update={"dossier": rewritten}), _principal()
    )

    assert isinstance(response, JSONResponse)
    assert response.status_code == 422
