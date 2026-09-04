"""The UBO-graph surfaces: server-derived tenancy, human-review-console routing and the read/write
split.

Driven against the real local ownership-graph fixture and the seeded dev personas (the
default ``analyst`` in tenant ``demo-bank``, the cross-tenant ``other-tenant`` persona in
``other-bank``), so the isolation assertions are not vacuous.

The invariants pinned here:

* the resolution's tenant and ACL are stamped from the VERIFIED principal, never from the request
  body, so two tenants can never collide in the human-review-console; * a caller holding no
  case-access role is refused (403), not served a structure; * POST is consequential: it always
  requires human review and is routed to human-review-console under rule R8, whereas GET returns the
  WALKED STRUCTURE ONLY and therefore routes nothing; and * the orchestrator degrades: a dead
  registry layer, a failed narration and a dead review console each leave the deterministic
  resolution standing.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cdd_sow_research.adapters.local.ownership_graph import LocalOwnershipGraphAdapter
from cdd_sow_research.api import deps
from cdd_sow_research.api.app import app
from cdd_sow_research.api.security import AuthenticatedContext, get_authenticated_context
from cdd_sow_research.config import Settings, build_container
from cdd_sow_research.domain.errors import CaseAccessDeniedError
from cdd_sow_research.domain.identity import Principal
from cdd_sow_research.domain.models import (
    ControlBasis,
    RegistryHop,
    Subject,
    SubjectType,
    UboResolution,
)
from cdd_sow_research.domain.services import UboGraphService

_SUBJECT = {
    "id": "acme",
    "name": "Acme Holdings Pte Ltd (FICTIONAL)",
    "type": "entity",
    "jurisdiction": "SG",
}


class _RecordingRouter:
    """Stands in for the human-review-console hand-off so the test never needs a live console."""

    def __init__(self, *, fail: bool = False) -> None:
        self.routed: list[UboResolution] = []
        self._fail = fail

    def route(self, case: Any, *, maker: str) -> None:  # pragma: no cover - unused here
        raise AssertionError("the dossier path is not exercised by these tests")

    def route_monitoring(self, assessment: Any, *, maker: str) -> None:  # pragma: no cover
        raise AssertionError("the perpetual-KYC path is not exercised by these tests")

    def route_ownership(self, resolution: UboResolution, *, maker: str) -> None:
        if self._fail:
            raise RuntimeError("the review console is unavailable (FICTIONAL)")
        self.routed.append(resolution)


class _RecordingAudit:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def record(self, event: Any) -> None:
        self.events.append(event)

    def record_once(self, event_id: str, event: Any) -> None:  # pragma: no cover - unused
        self.events.append(event)


def _service(
    *, router: _RecordingRouter, audit: _RecordingAudit, graph_port: Any = None
) -> UboGraphService:
    settings = Settings.load("config/settings.yaml")
    container = build_container(settings)
    return UboGraphService.from_policy(
        settings.policy,
        ownership_graph=graph_port or LocalOwnershipGraphAdapter(settings),
        review_router=router,
        audit=audit,
        tracer=container.tracer,
        redaction=container.redaction,
        llm=container.llm,
    )


@pytest.fixture()
def wiring() -> Iterator[tuple[TestClient, _RecordingRouter, _RecordingAudit]]:
    router = _RecordingRouter()
    audit = _RecordingAudit()
    service = _service(router=router, audit=audit)
    app.dependency_overrides[deps.get_ubo_graph_service] = lambda: service
    yield TestClient(app, client=("127.0.0.1", 50000)), router, audit
    app.dependency_overrides.clear()


def _resolve(client: TestClient, persona: str = "analyst", **body: Any):
    return client.post(
        "/v1/ubo-graph",
        json={"subject": _SUBJECT, **body},
        headers={"X-Dev-Persona": persona},
    )


# --------------------------------------------------------------------------- #
# POST: the consequential verb
# --------------------------------------------------------------------------- #
def test_a_resolution_is_tenant_stamped_reviewed_and_routed(wiring):
    client, router, audit = wiring
    resp = _resolve(client, as_of="2026-08-07")
    assert resp.status_code == 200
    body = resp.json()

    assert body["tenant"] == "demo-bank"
    assert body["requires_human_review"] is True
    assert body["routed_to_hrz7"] is True
    assert body["control_basis"] != ControlBasis.NONE.value
    assert router.routed and router.routed[0].subject_id == "acme"
    assert router.routed[0].acl == ("case:acme", "tenant:demo-bank")
    assert audit.events and audit.events[0].action == "ubo_graph.resolve"
    assert audit.events[0].metadata["requires_human_review"] == "true"


def test_the_response_shows_the_multiplication_behind_every_percentage(wiring):
    client, _router, _audit = wiring
    body = _resolve(client, as_of="2026-08-07").json()

    priced = [f for f in body["findings"] if f["paths"]]
    assert priced, "a resolved structure must expose at least one equity path"
    for finding in priced:
        for path in finding["paths"]:
            assert " = " in path["arithmetic"]
            assert path["steps"], "a path must name the hops it multiplied"
            assert path["citations"], "an unsourced percentage is not usable evidence"


def test_the_run_is_replayable_through_the_api(wiring):
    client, _router, _audit = wiring
    first = _resolve(client, as_of="2026-08-07").json()
    second = _resolve(client, as_of="2026-08-07").json()
    assert first == second


def test_a_bad_as_of_is_rejected(wiring):
    client, _router, _audit = wiring
    assert _resolve(client, as_of="not-a-date").status_code == 422


def test_the_tenant_comes_from_the_principal_not_the_request(wiring):
    """Two tenants resolving the same subject id produce two separately-scoped items."""
    client, router, _audit = wiring
    assert _resolve(client).status_code == 200
    assert _resolve(client, persona="other-tenant").status_code == 200

    assert [r.tenant for r in router.routed] == ["demo-bank", "other-bank"]
    assert router.routed[1].acl == ("case:acme", "tenant:other-bank")


def test_a_caller_with_no_case_access_role_is_denied(wiring):
    client, router, _audit = wiring
    stranger = Principal(
        subject="nobody@example.test",
        principals=("group:cafeteria",),
        tenant="demo-bank",
        assurance="local-demo",
        source="local-persona:analyst",
    )
    app.dependency_overrides[get_authenticated_context] = lambda: AuthenticatedContext(
        principal=stranger, evidence={}
    )
    try:
        assert _resolve(client).status_code == 403
        read = client.get("/v1/ubo-graph/acme", headers={"X-Dev-Persona": "analyst"})
        assert read.status_code == 403
    finally:
        app.dependency_overrides.pop(get_authenticated_context, None)
    assert router.routed == [], "a denied caller must not reach the review console"


def test_routes_require_authentication(wiring):
    client, _router, _audit = wiring
    settings = Settings.load("config/settings.yaml")
    if settings.identity_mode != "local-persona":  # pragma: no cover - profile-dependent
        pytest.skip("authentication shape differs outside the local-persona profile")
    assert _resolve(client, persona="not-a-real-persona").status_code in (401, 403)


# --------------------------------------------------------------------------- #
# GET: evidence, not a decision
# --------------------------------------------------------------------------- #
def test_the_fetch_endpoint_returns_the_structure_and_routes_nothing(wiring):
    client, router, audit = wiring
    resp = client.get(
        "/v1/ubo-graph/acme",
        params={"name": _SUBJECT["name"], "jurisdiction": "SG", "as_of": "2026-08-07"},
        headers={"X-Dev-Persona": "analyst"},
    )
    assert resp.status_code == 200
    body = resp.json()

    assert body["nodes"] and body["edges"]
    assert body["depth"] >= 1
    assert len(body["jurisdictions"]) > 1, "the fixture must span more than one jurisdiction"
    assert all(n["citations"] for n in body["nodes"] if n["depth"] > 0)
    # A read carries no verdict, so it engages neither maker-checker nor rule R8.
    assert set(body) == {
        "root_id",
        "root_name",
        "nodes",
        "edges",
        "depth",
        "truncated",
        "unresolved_ids",
        "jurisdictions",
        "as_of",
    }
    assert router.routed == []
    assert audit.events == []


def test_the_fetch_endpoint_rejects_a_bad_as_of(wiring):
    client, _router, _audit = wiring
    resp = client.get(
        "/v1/ubo-graph/acme",
        params={"as_of": "yesterday"},
        headers={"X-Dev-Persona": "analyst"},
    )
    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# Graceful degradation: the deterministic resolution always survives
# --------------------------------------------------------------------------- #
class _DeadRegistry:
    def hop(self, entity_name: str, jurisdiction: str) -> RegistryHop:
        raise RuntimeError("the registry is unavailable (FICTIONAL)")


def _subject() -> Subject:
    return Subject(
        id="acme",
        name=_SUBJECT["name"],
        type=SubjectType.ENTITY,
        jurisdiction="SG",
        tenant="demo-bank",
    )


def test_a_dead_registry_degrades_to_an_unresolved_layer():
    router, audit = _RecordingRouter(), _RecordingAudit()
    service = _service(router=router, audit=audit, graph_port=_DeadRegistry())

    resolution = service.resolve(_subject(), actor="tester", as_of=date(2026, 8, 7))

    assert resolution.graph is not None
    assert resolution.graph.unresolved_ids, "an unreadable layer must be recorded as opaque"
    assert "unresolved_layer" in resolution.flag_kinds
    assert resolution.requires_human_review is True
    assert router.routed, "an unresolvable structure still needs a human to see it"


def test_a_dead_review_console_leaves_the_resolution_standing():
    router, audit = _RecordingRouter(fail=True), _RecordingAudit()
    service = _service(router=router, audit=audit)

    resolution = service.resolve(_subject(), actor="tester", as_of=date(2026, 8, 7))

    assert resolution.routed_to_hrz7 is False
    assert resolution.requires_human_review is True
    assert audit.events[0].metadata["routed_to_hrz7"] == "false"


def test_a_failed_narration_is_discarded_not_rendered():
    class _BadLlm:
        def generate(self, request: Any) -> Any:
            raise RuntimeError("the model is unavailable (FICTIONAL)")

        def classify(self, text: str, labels: list[str]) -> str:  # pragma: no cover - unused
            return ""

    settings = Settings.load("config/settings.yaml")
    container = build_container(settings)
    router, audit = _RecordingRouter(), _RecordingAudit()
    service = UboGraphService.from_policy(
        settings.policy,
        ownership_graph=LocalOwnershipGraphAdapter(settings),
        review_router=router,
        audit=audit,
        tracer=container.tracer,
        redaction=container.redaction,
        llm=_BadLlm(),
    )

    resolution = service.resolve(_subject(), actor="tester", as_of=date(2026, 8, 7))

    assert resolution.narrative == ""
    assert resolution.beneficial_owners, "the deterministic answer does not depend on prose"


def test_the_offline_narrator_describes_ownership_not_a_re_score():
    """Both narrators share one schema; the wrong branch would caption the wrong module."""
    router, audit = _RecordingRouter(), _RecordingAudit()
    service = _service(router=router, audit=audit)

    resolution = service.resolve(_subject(), actor="tester", as_of=date(2026, 8, 7))

    assert "beneficial-ownership structure" in resolution.narrative
    assert "Perpetual-KYC" not in resolution.narrative
    assert "requires human review" in resolution.narrative


def test_the_entitlement_error_is_a_denial_not_a_crash():
    """``case_scope`` is the gate; the route maps its refusal to 403, never to a 500."""
    stranger = Principal(
        subject="nobody@example.test",
        principals=(),
        tenant="demo-bank",
        assurance="local-demo",
        source="local-persona:analyst",
    )
    from cdd_sow_research.domain import entitlements

    with pytest.raises(CaseAccessDeniedError):
        entitlements.case_scope(stranger, "acme")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
