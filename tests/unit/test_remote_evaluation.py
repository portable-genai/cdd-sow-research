"""Contract test for the model-quality-gate remote-evaluation adapter (RemoteEvaluationAdapter).

Guards the corrected model-quality-gate client contract (see the shared evaluation client contract):
a structured ``target``, top-level ``dataset_id`` equal to ``target.dataset_id``, selection by the
registered ``doc1-cdd-sow`` bundle with no bare metric names, response parsing from ``results[]``,
and a POST promotion gate.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from cdd_sow_research.adapters.platform.remote_evaluation import (
    RemoteEvaluationAdapter,
    RemoteEvaluationError,
)
from cdd_sow_research.config import LocalSettings, Settings

_BASE = "http://localhost:8084"


def _adapter() -> RemoteEvaluationAdapter:
    return RemoteEvaluationAdapter(
        Settings(profile="platform", local=LocalSettings(db_path=":memory:", audit_path=":memory:"))
    )


# Every score/threshold/passed row is internally CONSISTENT, because the client
# re-derives each verdict and raises on contradiction rather than trusting the flag.
_RESULTS_BODY = {
    "target": {"model": "gemini-3.5-flash", "prompt_version": "v1", "dataset_id": "golden_cases"},
    "results": [
        {"metric": "sow_groundedness", "score": 0.91, "threshold": 0.80, "passed": True},
        {"metric": "risk_band_accuracy", "score": 0.70, "threshold": 0.85, "passed": False},
        {"metric": "citation_accuracy", "score": 0.95, "threshold": 0.90, "passed": True},
        {"metric": "pii_safety", "score": 1.0, "threshold": 0.99, "passed": True},
    ],
    "n_examples": 24,
    "run_id": "run-fictional-0002",
    "dataset_version": "golden_cases@2026-08-01",
    "dataset_digest": "sha256:feedfacefeedfacefeedfacefeedfacefeedfacefeedfacefeedfacefeedface",
    "evaluator": "hrz4-ai-quality (FICTIONAL)",
    "schema_version": "v1",
    "artifact_refs": ["gs://fictional-hrz4-evidence/run-fictional-0002/report.json"],
    "attested": True,
    "passed": False,
}


def _passing_eval_report() -> dict:
    """Attested evaluation evidence in the full hardened shape, obviously fictional."""
    return {
        "results": [
            {"metric": "sow_groundedness", "score": 0.93, "threshold": 0.80, "passed": True},
            {"metric": "risk_band_accuracy", "score": 0.90, "threshold": 0.85, "passed": True},
            {"metric": "citation_accuracy", "score": 0.95, "threshold": 0.90, "passed": True},
            {"metric": "pii_safety", "score": 1.0, "threshold": 0.99, "passed": True},
        ],
        "n_examples": 24,
        "run_id": "run-fictional-0001",
        "dataset_version": "golden_cases@2026-08-01",
        "dataset_digest": "sha256:feedfacefeedfacefeedfacefeedfacefeedfacefeedfacefeedfacefeedface",
        "evaluator": "hrz4-ai-quality (FICTIONAL)",
        "schema_version": "v1",
        "artifact_refs": ["gs://fictional-hrz4-evidence/run-fictional-0001/report.json"],
        "attested": True,
    }


#: The complete GateDecision the promotion gate now demands. A naked ``{"passed": true}``
#: is REFUSED: the client re-derives the verdict from the evidence and every layer must
#: agree (metric rows with their thresholds, the attested eval report with durable
#: identifiers and artifact refs, a red-team report whose aggregate matches its rows, and
#: the model-card and MRM references).
_GATE_BODY = {
    "passed": True,
    "eval_report": _passing_eval_report(),
    "redteam_report": {
        "passed": True,
        "results": [
            {"case": "prompt-injection-01", "passed": True, "blocked": True},
            {"case": "pii-exfil-01", "passed": True, "blocked": True},
        ],
    },
    "model_card_ref": "gs://fictional-hrz4-evidence/model-cards/doc1-cdd-sow.md",
    "mrm_evidence_ref": "gs://fictional-hrz4-evidence/mrm/doc1-cdd-sow-2026-08.json",
}


@respx.mock
def test_evaluate_sends_structured_target_and_parses_results():
    route = respx.post(f"{_BASE}/v1/evaluations").mock(
        return_value=httpx.Response(200, json=_RESULTS_BODY)
    )
    report = _adapter().evaluate("eval/datasets/golden_cases.jsonl")

    sent = route.calls.last.request
    body = _json(sent)
    # Structured target, not a bare string.
    assert body["target"]["model"] == "gemini-3.5-flash"
    assert body["target"]["dataset_id"] == "golden_cases"
    # Top-level dataset_id equals target.dataset_id (model-quality-gate 422s on divergence).
    assert body["dataset_id"] == body["target"]["dataset_id"] == "golden_cases"
    # Selection by registered bundle; NO bare metric names on the wire.
    assert body["bundle"] == "doc1-cdd-sow"
    assert "metrics" not in body
    # Parsed from results[] (the real key), thresholds passed through.
    assert report.passed is False
    metrics = {r.metric: (r.threshold, r.passed) for r in report.results}
    assert metrics["risk_band_accuracy"] == (0.85, False)
    assert metrics["pii_safety"] == (0.99, True)


@respx.mock
def test_evaluate_preserves_the_attested_promotion_evidence():
    """The adapter must return the client's report UNCHANGED.

    Rebuilding a locally declared ``EvalReport`` from three fields throws away exactly the
    evidence the client has just validated. Every assertion below fails against such a mapper,
    because the rebuilt report carries the defaults instead.
    """
    respx.post(f"{_BASE}/v1/evaluations").mock(return_value=httpx.Response(200, json=_RESULTS_BODY))
    report = _adapter().evaluate("eval/datasets/golden_cases.jsonl")

    assert report.run_id == "run-fictional-0002"
    assert report.dataset_version == "golden_cases@2026-08-01"
    assert report.dataset_digest == _RESULTS_BODY["dataset_digest"]
    assert report.evaluator == "hrz4-ai-quality (FICTIONAL)"
    assert report.schema_version == "v1"
    assert report.artifact_refs == tuple(_RESULTS_BODY["artifact_refs"])
    assert report.attested is True
    # The dataset the caller asked about still names the report, as before.
    assert report.dataset == "eval/datasets/golden_cases.jsonl"


@respx.mock
def test_gate_accepts_only_a_full_consistent_gate_decision():
    route = respx.post(f"{_BASE}/v1/gate").mock(return_value=httpx.Response(200, json=_GATE_BODY))
    assert _adapter().gate("eval/datasets/golden_cases.jsonl") is True
    body = _json(route.calls.last.request)
    assert body["bundle"] == "doc1-cdd-sow"
    assert body["dataset_id"] == body["target"]["dataset_id"]


@respx.mock
def test_gate_REFUSES_a_naked_boolean_with_no_evidence():
    """The unhardened response shape. Accepting it is how a promotion gets certified by
    nothing, so the refusal is the contract, not an inconvenience."""
    respx.post(f"{_BASE}/v1/gate").mock(return_value=httpx.Response(200, json={"passed": True}))
    with pytest.raises(RemoteEvaluationError):
        _adapter().gate("eval/datasets/golden_cases.jsonl")


@respx.mock
def test_gate_REFUSES_an_unattested_report_even_when_every_metric_passes():
    body = {**_GATE_BODY, "eval_report": {**_passing_eval_report(), "attested": False}}
    respx.post(f"{_BASE}/v1/gate").mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(RemoteEvaluationError):
        _adapter().gate("eval/datasets/golden_cases.jsonl")


@respx.mock
def test_non_2xx_raises():
    respx.post(f"{_BASE}/v1/evaluations").mock(return_value=httpx.Response(422, text="bad bundle"))
    with pytest.raises(RemoteEvaluationError):
        _adapter().evaluate("eval/datasets/golden_cases.jsonl")


def _json(request: httpx.Request) -> dict:
    import json

    return json.loads(request.content.decode("utf-8"))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
