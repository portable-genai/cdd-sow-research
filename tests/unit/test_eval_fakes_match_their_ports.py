"""The gate's inlined fakes must satisfy the ports they stand in for, structurally.

A fake that has drifted from its port is a gate measuring a pipeline the product does not have,
and the drift is invisible in exactly the cases that matter. This is not hypothetical: the eval's
`FakeKnowledgeBase.ingest` was missing the `page_texts` parameter the port declares, so every
document ingestion in the gate raised. `CddService` catches an ingestion failure and continues
with "this document will not ground any citation", which is right for the product and fatal for
the gate: the run stayed green while no case document grounded anything at all, and the grounding
metrics scored the adverse-media and ownership citations alone.

`runtime_checkable` Protocols only check method NAMES, so `isinstance` would have passed against
the broken fake. These assertions compare signatures, which is where the drift was.
"""

from __future__ import annotations

import inspect

import pytest
from eval import run_eval

from cdd_sow_research.ports.knowledge_base import KnowledgeBaseClientPort


def _required_parameters(func: object) -> list[str]:
    """Parameter names a caller may pass, minus `self`."""
    return [
        name
        for name, parameter in inspect.signature(func).parameters.items()  # type: ignore[arg-type]
        if name != "self" and parameter.kind is not inspect.Parameter.VAR_KEYWORD
    ]


def test_the_fake_knowledge_base_accepts_every_argument_the_port_declares() -> None:
    port = _required_parameters(KnowledgeBaseClientPort.ingest)
    fake = _required_parameters(run_eval.FakeKnowledgeBase.ingest)
    missing = [name for name in port if name not in fake]
    assert not missing, (
        f"FakeKnowledgeBase.ingest cannot accept {missing}, which the port declares. Every "
        "ingestion in the gate would raise, the service would continue without grounding, and "
        "the run would stay green over a pipeline the product does not have."
    )


def test_the_gate_actually_ingests_every_case_document() -> None:
    """The positive form, because a matching signature is not the same as a working call.

    Counting successful ingestions is what distinguishes "the fake accepts the argument" from
    "the documents reached the index", and only the second one grounds a citation.
    """
    examples = run_eval.load_golden(run_eval.DEFAULT_DATASET)
    adapters = run_eval._build_adapters(examples)
    service = run_eval._make_service(adapters)
    for example in examples:
        case = service.assess(run_eval._case_input(example), actor="eval-bot")
        assert case is not None


def test_a_fake_that_drops_a_port_argument_is_caught() -> None:
    """The proof that the check above can fail, rather than being a tick over an empty set."""

    class DriftedFake:
        def ingest(self, document, content, acl_tags):  # type: ignore[no-untyped-def]
            return None

    port = _required_parameters(KnowledgeBaseClientPort.ingest)
    fake = _required_parameters(DriftedFake.ingest)
    assert [name for name in port if name not in fake] == ["page_texts"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
