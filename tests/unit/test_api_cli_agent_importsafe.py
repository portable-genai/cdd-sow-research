"""Import-safety + wiring tests for the API, CLI and agent layers.

The on-prem / test profile installs **no Google Cloud SDK**, so importing these wiring
modules (and constructing the FastAPI app, the Typer CLI, the AgentCard) must never pull
in ``google.adk`` / ``google-cloud-*``. The API endpoints are also exercised end-to-end
against the on-prem fakes via FastAPI dependency overrides.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient
from tests.conftest import (
    FakeAdverseMedia,
    FakeAudit,
    FakeCompliance,
    FakeExtraction,
    FakeGuardrail,
    FakeKnowledgeBase,
    FakeLLM,
    FakeRedaction,
    FakeRegistry,
    FakeTracer,
)


def test_api_app_imports_without_gcp_sdk():
    module = importlib.import_module("cdd_sow_research.api.app")
    assert module.app is not None


def test_cli_imports_without_gcp_sdk():
    module = importlib.import_module("cdd_sow_research.cli.main")
    assert module.app is not None


def test_agent_root_imports_without_adk():
    # The lazy root_agent proxy must import with no ADK installed.
    module = importlib.import_module("cdd_sow_research.agent.root_agent")
    assert repr(module.root_agent)  # touching repr must not build the agent


def test_agent_card_is_pure():
    from cdd_sow_research.agent.agent_card import build_agent_card
    from cdd_sow_research.config import Settings

    card = build_agent_card(Settings.load("config/settings.yaml"))
    assert card.name == "cdd-sow-research"
    skill_ids = {s.id for s in card.skills}
    assert {"assess_cdd", "build_source_of_wealth", "scan_adverse_media", "resolve_ownership"} <= (
        skill_ids
    )


# --------------------------------------------------------------------------- #
# End-to-end API via dependency overrides (on-prem fakes).
# --------------------------------------------------------------------------- #
@pytest.fixture
def client():
    from cdd_sow_research.api import deps
    from cdd_sow_research.api.app import app
    from cdd_sow_research.domain.services import CddService

    kb = FakeKnowledgeBase()
    cdd = CddService(
        extraction=FakeExtraction(),
        knowledge_base=kb,
        adverse_media=FakeAdverseMedia(),
        registry=FakeRegistry(),
        compliance=FakeCompliance(),
        llm=FakeLLM(),
        guardrail=FakeGuardrail(),
        redaction=FakeRedaction(),
        tracer=FakeTracer(),
        audit=FakeAudit(),
    )
    app.dependency_overrides[deps.get_cdd_service] = lambda: cdd
    yield TestClient(app, client=("127.0.0.1", 50000))
    app.dependency_overrides.clear()


def test_healthz(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["region"] == "asia-southeast1"
    assert response.json()["demo_only"] is True
    assert response.json()["production_ready"] is False


def test_local_capability_manifest_is_functional_without_managed_assurance(client):
    response = client.get("/v1/capabilities")
    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == "capability-manifest/v1"
    assert body["demo_only"] is True
    by_name = {item["name"]: item for item in body["capabilities"]}
    assert by_name["cdd-workflow"]["available"] is True
    assert by_name["evaluation-gate"]["assurance"] == "demo-only"
    assert by_name["immutable-audit"]["available"] is False
    assert by_name["observability"]["available"] is False
    assert by_name["model-armor"]["available"] is False


def test_agent_card_endpoint(client):
    response = client.get("/.well-known/agent-card.json")
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "cdd-sow-research"
    assert {s["id"] for s in body["skills"]} >= {"assess_cdd", "scan_adverse_media"}


def test_cdd_endpoint_returns_dossier(client):
    response = client.post(
        "/v1/cdd",
        json={
            "subject": {
                "id": "subj-acme",
                "name": "Acme Holdings Pte Ltd (FICTIONAL)",
                "type": "entity",
                "jurisdiction": "SG",
            },
            "documents": [
                {"id": "doc-1", "doc_type": "registry_extract", "acl_tags": ["case:subj-acme"]}
            ],
            "actor": "analyst@bank.test",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["requires_human_review"] is True
    assert body["rating"]["band"]
    assert body["sow"]["narrative"]
    assert body["sow"]["citations"], "the dossier narrative must carry citations"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
