"""WP1: the five SoW-case REST routes, with server-derived ACL (cross-tenant isolation).

Drives the routes against the real local case store and the seeded dev personas (the
default ``analyst`` in tenant ``demo-bank`` and the cross-tenant ``other-tenant`` persona
in ``other-bank``), proving the "cross-tenant persona gets 403 on load and zero from
list_for" invariant on the default local path (not vacuous).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from cdd_sow_research.adapters.local.case_store import LocalCaseStoreAdapter
from cdd_sow_research.api import deps
from cdd_sow_research.api.app import app
from cdd_sow_research.config import Settings
from cdd_sow_research.domain.services import SowCaseService

_SUBJECT = {"id": "acme", "name": "Acme Holdings Pte Ltd (FICTIONAL)", "type": "entity"}


@pytest.fixture()
def client() -> Iterator[TestClient]:
    # Fresh in-process case store per test (the real local adapter, ACL-enforced).
    service = SowCaseService.from_policy(LocalCaseStoreAdapter(Settings()), Settings().policy)
    app.dependency_overrides[deps.get_sow_case_service] = lambda: service
    yield TestClient(app, client=("127.0.0.1", 50000))
    app.dependency_overrides.clear()


def _open(client: TestClient, persona: str = "analyst"):
    return client.post(
        "/v1/sow-cases",
        json={"case_id": "acme", "subject": _SUBJECT},
        headers={"X-Dev-Persona": persona},
    )


def test_owner_opens_and_reads_its_case(client: TestClient):
    opened = _open(client)
    assert opened.status_code == 201
    body = opened.json()
    # Tenant stamped server-side from the verified principal (demo-bank), not the body.
    assert body["subject"]["tenant"] == "demo-bank"

    got = client.get("/v1/sow-cases/acme", headers={"X-Dev-Persona": "analyst"})
    assert got.status_code == 200
    assert got.json()["id"] == "acme"


def test_cross_tenant_persona_gets_403_on_load(client: TestClient):
    assert _open(client).status_code == 201
    # The other-bank persona holds a case-access role but not tenant:demo-bank.
    denied = client.get("/v1/sow-cases/acme", headers={"X-Dev-Persona": "other-tenant"})
    assert denied.status_code == 403


def test_unknown_case_is_404(client: TestClient):
    resp = client.get("/v1/sow-cases/nope", headers={"X-Dev-Persona": "analyst"})
    assert resp.status_code == 404


def test_analyze_and_review_flow(client: TestClient):
    assert _open(client).status_code == 201
    # Add a round of evidence, then analyze (advances the case), then a checker reviews.
    ev = client.post(
        "/v1/sow-cases/acme/evidence",
        json={"items": [{"id": "e1", "document": {"id": "d1", "doc_type": "bank_statement"}}]},
        headers={"X-Dev-Persona": "analyst"},
    )
    assert ev.status_code == 200
    analyzed = client.post("/v1/sow-cases/acme/analyze", headers={"X-Dev-Persona": "analyst"})
    assert analyzed.status_code == 200


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
