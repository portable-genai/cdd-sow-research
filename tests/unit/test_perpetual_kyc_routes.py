"""The perpetual-KYC REST surface: server-derived ACL, cross-tenant isolation, human-review-console
routing.

Drives the two routes against the real local monitoring store and the seeded dev personas
(the default ``analyst`` in tenant ``demo-bank`` and the cross-tenant ``other-tenant``
persona in ``other-bank``), so the isolation assertions are not vacuous.

The invariants pinned here:

* the record's tenant is stamped from the VERIFIED principal, never the request body;
* a cross-tenant caller reading a subject's baseline gets 403 (not 404, not another
  tenant's history), and their queue listing is empty; and
* the response always carries ``requires_human_review`` and the human-review-console routing flag.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cdd_sow_research.adapters.local.monitoring_store import LocalMonitoringStoreAdapter
from cdd_sow_research.api import deps
from cdd_sow_research.api.app import app
from cdd_sow_research.config import Settings, build_container
from cdd_sow_research.domain.models import PerpetualKycAssessment
from cdd_sow_research.domain.services import PerpetualKycService

_SUBJECT = {
    "id": "acme",
    "name": "Acme Holdings Pte Ltd (FICTIONAL)",
    "type": "entity",
    "jurisdiction": "SG",
}


class _RecordingRouter:
    """Stands in for the human-review-console hand-off so the test never needs a live console."""

    def __init__(self) -> None:
        self.routed: list[PerpetualKycAssessment] = []

    def route(self, case: Any, *, maker: str) -> None:  # pragma: no cover - unused here
        raise AssertionError("the dossier path is not exercised by these tests")

    def route_monitoring(self, assessment: PerpetualKycAssessment, *, maker: str) -> None:
        self.routed.append(assessment)


class _RecordingAudit:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def record(self, event: Any) -> None:
        self.events.append(event)

    def record_once(self, event_id: str, event: Any) -> None:  # pragma: no cover - unused
        self.events.append(event)


@pytest.fixture()
def wiring() -> Iterator[tuple[TestClient, _RecordingRouter, _RecordingAudit]]:
    settings = Settings.load("config/settings.yaml")
    container = build_container(settings)
    router = _RecordingRouter()
    audit = _RecordingAudit()
    service = PerpetualKycService.from_policy(
        settings.policy,
        sanctions=container.sanctions,
        adverse_media=container.adverse_media,
        registry=container.registry,
        store=LocalMonitoringStoreAdapter(settings),
        review_router=router,
        audit=audit,
        tracer=container.tracer,
        redaction=container.redaction,
        llm=container.llm,
    )
    app.dependency_overrides[deps.get_perpetual_kyc_service] = lambda: service
    yield TestClient(app, client=("127.0.0.1", 50000)), router, audit
    app.dependency_overrides.clear()


def _run(client: TestClient, persona: str = "analyst", **body: Any):
    return client.post(
        "/v1/perpetual-kyc",
        json={"subject": _SUBJECT, **body},
        headers={"X-Dev-Persona": persona},
    )


def test_a_cycle_is_tenant_stamped_reviewed_and_routed(wiring):
    client, router, audit = wiring
    resp = _run(client, as_of="2026-08-05")
    assert resp.status_code == 200
    body = resp.json()

    # Tenant comes from the verified principal, not from the request body.
    assert body["tenant"] == "demo-bank"
    assert body["requires_human_review"] is True
    assert body["queue_item"]["requires_human_review"] is True
    assert body["queue_item"]["routed_to_hrz7"] is True
    assert body["queue_item"]["reasons"], "a queue item must explain itself"
    assert router.routed and router.routed[0].subject_id == "acme"
    assert audit.events and audit.events[0].action == "perpetual_kyc.rescore"


def test_the_run_is_replayable_through_the_api(wiring):
    client, _router, _audit = wiring
    first = _run(client, as_of="2026-08-05").json()
    second = _run(client, as_of="2026-08-05").json()
    # The second run sees the baseline the first established, so nothing moved.
    assert second["score"] == first["score"]
    assert second["score_delta"] == 0.0
    assert all(s["change"] == "persisting" for s in second["signals"])


def test_a_bad_as_of_is_rejected(wiring):
    client, _router, _audit = wiring
    assert _run(client, as_of="not-a-date").status_code == 422


def test_cross_tenant_caller_is_denied_the_baseline(wiring):
    client, _router, _audit = wiring
    assert _run(client).status_code == 200
    # The other-bank persona holds a case-access role but not tenant:demo-bank, so the
    # stored baseline is refused outright rather than silently treated as absent.
    denied = _run(client, persona="other-tenant")
    assert denied.status_code == 403


def test_the_queue_is_tenant_scoped(wiring):
    client, _router, _audit = wiring
    assert _run(client).status_code == 200

    mine = client.get("/v1/perpetual-kyc/queue", headers={"X-Dev-Persona": "analyst"})
    assert mine.status_code == 200
    items = mine.json()["items"]
    assert [i["subject_id"] for i in items] == ["acme"]

    theirs = client.get("/v1/perpetual-kyc/queue", headers={"X-Dev-Persona": "other-tenant"})
    assert theirs.status_code == 200
    assert theirs.json()["items"] == []


def test_routes_require_authentication(wiring):
    client, _router, _audit = wiring
    settings = Settings.load("config/settings.yaml")
    if settings.identity_mode != "local-persona":  # pragma: no cover - profile-dependent
        pytest.skip("authentication shape differs outside the local-persona profile")
    # An unknown persona is not a verified principal, so the protected route refuses it.
    assert _run(client, persona="not-a-real-persona").status_code in (401, 403)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
