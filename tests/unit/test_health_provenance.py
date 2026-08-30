"""The provenance the UI banner states, derived server-side and never guessed.

Every page names where the runtime sits and which model answers (org decision,
2026-08-30). The UI reads both from ``/v1/healthz``; these tests hold the derivation so
a banner claim is a property of the configuration, not of the frontend.
"""

from __future__ import annotations

from cdd_sow_research.api.app import _generator_model
from cdd_sow_research.config import Settings


def test_the_local_profile_names_its_stub_not_a_model() -> None:
    """A banner that called the deterministic stub a model would claim inference
    that never happens."""

    assert _generator_model(Settings(profile="local")) == "deterministic-offline-stub"


def test_live_and_managed_name_the_same_gemini_model() -> None:
    """`live` differs from the managed profiles by runtime, not by generator: same
    model id, which is what lets the F4 pair compare runtimes rather than models."""

    live = _generator_model(Settings(profile="live"))
    gcp = _generator_model(Settings(profile="gcp"))
    assert live == gcp
    assert live.startswith("gemini-"), live


def test_healthz_carries_runtime_and_model() -> None:
    from fastapi.testclient import TestClient

    from cdd_sow_research.api.app import app

    client = TestClient(app, client=("127.0.0.1", 50000))
    payload = client.get("/v1/healthz", headers={"X-Dev-Persona": "analyst"}).json()

    assert payload["runtime"] in {"gcp", "local"}
    assert payload["generator_model"]
