"""Priority 1 compatibility: CDD_STANDALONE does not select channel or auth."""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

import cdd_sow_research.api.app as appmod
from cdd_sow_research.api import deps


@pytest.fixture(autouse=True)
def _restore_default(monkeypatch: pytest.MonkeyPatch):
    yield
    monkeypatch.undo()
    deps.get_container.cache_clear()
    importlib.reload(appmod)


def test_local_default_reports_independent_selectors_and_hides_oidc_routes() -> None:
    deps.get_container.cache_clear()
    client = TestClient(appmod.app, client=("127.0.0.1", 50000))

    health = client.get("/healthz").json()
    assert health["profile"] == "local"
    assert health["identity_mode"] == "local-persona"
    assert health["channel_mode"] == "standalone"
    assert health["mode"] == "application"
    assert len(health["configuration_hash"]) == 64
    assert client.get("/auth/login", follow_redirects=False).status_code == 404


def test_legacy_flag_does_not_change_the_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CDD_STANDALONE", "false")
    monkeypatch.setenv("CDD_IDENTITY_PROFILE", "iap")
    monkeypatch.setenv("CDD_CHANNEL_PROFILE", "native")
    monkeypatch.setenv("CDD_PROFILE", "gcp")
    deps.get_container.cache_clear()
    importlib.reload(appmod)
    client = TestClient(appmod.app, client=("127.0.0.1", 50000))

    health = client.get("/healthz").json()
    assert health["identity_mode"] == "iap"
    assert health["channel_mode"] == "native"
    assert health["mode"] == "platform"
    assert client.get("/auth/login", follow_redirects=False).status_code == 404


def test_legacy_platform_control_conflicts_with_local_persona_at_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CDD_STANDALONE", "false")
    monkeypatch.setenv("CDD_IDENTITY_PROFILE", "local-persona")
    monkeypatch.setenv("CDD_CHANNEL_PROFILE", "native")
    deps.get_container.cache_clear()
    importlib.reload(appmod)

    with (
        pytest.raises(RuntimeError, match="assigns identity control to the platform"),
        TestClient(appmod.app, client=("127.0.0.1", 50000)),
    ):
        pass


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
