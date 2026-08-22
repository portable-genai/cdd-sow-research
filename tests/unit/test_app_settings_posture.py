"""Application edge posture must come from the same YAML-loaded Settings instance."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from cdd_sow_research.api.app import _rate_per_minute, create_app
from cdd_sow_research.config import Settings

_DEV_ORIGIN = "http://localhost:3000"


def test_yaml_only_secure_identity_never_inherits_local_edge_defaults(
    tmp_path: Path,
    monkeypatch,
) -> None:
    for name in (
        "CDD_PROFILE",
        "CDD_IDENTITY_PROFILE",
        "CDD_CHANNEL_PROFILE",
        "CDD_CORS_ORIGINS",
        "CDD_FRAME_ANCESTORS",
        "CDD_RATE_LIMIT_PER_MINUTE",
        "CDD_MAX_BODY_BYTES",
    ):
        monkeypatch.delenv(name, raising=False)
    source = Path("config/settings.yaml").read_text()
    source = source.replace(
        "mode: ${CDD_IDENTITY_PROFILE:-}",
        "mode: iap",
        1,
    ).replace(
        "mode: ${CDD_CHANNEL_PROFILE:-}",
        "mode: standalone",
        1,
    )
    config_path = tmp_path / "secure-settings.yaml"
    config_path.write_text(source)
    settings = Settings.load(config_path)
    assert settings.identity_mode == "iap"

    with TestClient(create_app(settings), client=("127.0.0.1", 50000)) as client:
        response = client.get(
            "/healthz",
            headers={"Origin": "http://localhost:3000"},
        )
        preflight = client.options(
            "/v1/cdd",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "X-Dev-Persona",
            },
        )

    assert response.status_code == 200
    assert response.json()["identity_mode"] == "iap"
    assert response.headers["strict-transport-security"].startswith("max-age=")
    assert "access-control-allow-origin" not in response.headers
    assert preflight.status_code == 400


def _unconsented_settings(monkeypatch) -> Settings:
    """Load the shipped settings with NOTHING naming a profile: the fail-open condition."""
    for name in ("CDD_PROFILE", "CDD_IDENTITY_PROFILE", "CDD_CORS_ORIGINS"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings.load("config/settings.yaml")
    assert settings.profile_explicit is False
    return settings


def test_an_unconsented_run_gets_no_dev_origin_and_no_persona_header(monkeypatch) -> None:
    """An unset CDD_PROFILE must not be read as "chose local".

    ``local`` infers the ``local-persona`` identity mode, which would hand an unconsented
    process the localhost CORS allowlist, the ``X-Dev-Persona`` request header and a disabled
    rate limit. None of the three reaches it, because every relaxation reads
    ``exposure_identity_mode``.
    """
    settings = _unconsented_settings(monkeypatch)

    with TestClient(create_app(settings), client=("127.0.0.1", 50000)) as client:
        response = client.get("/healthz", headers={"Origin": _DEV_ORIGIN})
        preflight = client.options(
            "/v1/cdd",
            headers={
                "Origin": _DEV_ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "X-Dev-Persona",
            },
        )

    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers
    assert preflight.status_code == 400
    assert _rate_per_minute(settings) == 120


def test_an_unconsented_run_refuses_the_seeded_personas(monkeypatch) -> None:
    """The personas authenticate nobody, so an inherited local profile must not serve them."""
    settings = _unconsented_settings(monkeypatch)

    with TestClient(create_app(settings), client=("127.0.0.1", 50000)) as client:
        response = client.get("/v1/cases/case-1/documents")
        listed = client.get("/v1/personas")

    assert response.status_code == 401
    assert "CDD_PROFILE" in response.json()["detail"]
    assert listed.json() == []


def test_a_deliberate_local_run_still_serves_the_demo_posture(monkeypatch) -> None:
    """The companion case: choosing local deliberately keeps the offline demo working."""
    monkeypatch.setenv("CDD_PROFILE", "local")
    monkeypatch.delenv("CDD_CORS_ORIGINS", raising=False)
    settings = Settings.load("config/settings.yaml")
    assert settings.profile_explicit is True

    with TestClient(create_app(settings), client=("127.0.0.1", 50000)) as client:
        response = client.get("/healthz", headers={"Origin": _DEV_ORIGIN})
        listed = client.get("/v1/personas")

    assert response.headers["access-control-allow-origin"] == _DEV_ORIGIN
    assert [persona["id"] for persona in listed.json()] == [
        "analyst",
        "approver",
        "auditor",
        "other-tenant",
    ]


def test_an_emptied_cors_allowlist_refuses_instead_of_reopening_the_dev_origins(
    monkeypatch,
) -> None:
    """Set-and-empty is a THIRD state: it refuses, it does not fall back to the relaxation."""
    monkeypatch.setenv("CDD_PROFILE", "local")
    monkeypatch.setenv("CDD_CORS_ORIGINS", "")
    settings = Settings.load("config/settings.yaml")

    assert settings.web.cors_origins == ()
    assert settings.web.cors_origins_configured is True

    with TestClient(create_app(settings), client=("127.0.0.1", 50000)) as client:
        response = client.get("/healthz", headers={"Origin": _DEV_ORIGIN})

    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_early_body_limit_response_keeps_allowed_cors_and_security_headers() -> None:
    base = Settings.load("config/settings.yaml")
    settings = replace(
        base,
        web=replace(base.web, max_body_bytes=1, rate_limit_per_minute=0),
    )

    with TestClient(create_app(settings), client=("127.0.0.1", 50000)) as client:
        response = client.post(
            "/v1/cdd",
            content=b"too large",
            headers={"Origin": _DEV_ORIGIN},
        )

    assert response.status_code == 413
    assert response.headers["access-control-allow-origin"] == _DEV_ORIGIN
    assert response.headers["access-control-allow-credentials"] == "true"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_early_rate_limit_response_keeps_allowed_cors_and_security_headers() -> None:
    base = Settings.load("config/settings.yaml")
    settings = replace(
        base,
        web=replace(base.web, rate_limit_per_minute=1),
    )

    with TestClient(create_app(settings), client=("127.0.0.1", 50000)) as client:
        first = client.get("/healthz", headers={"Origin": _DEV_ORIGIN})
        limited = client.get("/healthz", headers={"Origin": _DEV_ORIGIN})

    assert first.status_code == 200
    assert limited.status_code == 429
    assert limited.headers["access-control-allow-origin"] == _DEV_ORIGIN
    assert limited.headers["access-control-allow-credentials"] == "true"
    assert limited.headers["x-content-type-options"] == "nosniff"
    assert limited.headers["retry-after"] == "60"
