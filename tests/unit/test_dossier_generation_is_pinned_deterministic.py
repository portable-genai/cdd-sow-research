"""Every call that decides a compared dossier field must be sampled deterministically.

The paired demonstration compares what `pairing.py` calls the DETERMINISTIC dossier, and the
comparison is only meaningful if each profile returns the same answer for the same inputs. On
2026-08-26 the deployment did not: two runs of the identical case, same subject and same
single-document corpus, minutes apart, returned `score` 0.5 then 0.0, `confidence` 0.4 then 1.0,
and four scorecard factors then none. The band held at `medium` both times, which is the only
reason a casual look would miss it.

The cause was in the shared builder rather than in any one service. `build_llm_request` defaulted
to `temperature=0.2`, so the risk rating, the source-of-wealth extraction, the UBO graph and the
perpetual-KYC pass all sampled. Every adapter that makes its own grounded call had already pinned
0.0 -- adverse media, ownership, the registry lookup, extraction -- so the domain's own builder
was the outlier, not the precedent.

**Temperature 0 is not a promise of determinism** and this file does not assert one: a hosted
model can still vary across batching and revisions. It is the strongest thing the caller controls,
and it is a precondition for the pair being a measurement rather than a sample. Whether the
deployment is actually reproducible is measured by running it twice, not by this test.
"""

from __future__ import annotations

import inspect

from cdd_sow_research.domain import _grounded as g


def test_the_shared_builder_does_not_sample_by_default() -> None:
    signature = inspect.signature(g.build_llm_request)

    assert signature.parameters["temperature"].default == 0.0


def test_a_built_request_carries_the_pinned_temperature() -> None:
    request = g.build_llm_request(
        system_instruction="s", user_content="u", model=None, response_schema=None
    )

    assert request.temperature == 0.0


def test_every_domain_service_deciding_a_compared_field_uses_the_shared_builder() -> None:
    """A service that hand-rolled its own request would sidestep the pin above.

    These four are the ones whose output reaches a field `pairing.py` compares, so the guard is
    on the source rather than on the value: asserting a temperature somewhere would pass against
    a fresh copy of the request-building code.
    """
    from cdd_sow_research.domain import (
        perpetual_kyc_service,
        risk_service,
        sow_service,
        ubo_graph_service,
    )

    for module in (risk_service, sow_service, ubo_graph_service, perpetual_kyc_service):
        source = inspect.getsource(module)
        assert "g.build_llm_request(" in source, module.__name__
        assert "temperature=" not in source, (
            f"{module.__name__} sets its own temperature; the pin belongs in one place"
        )
