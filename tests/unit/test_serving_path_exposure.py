"""The loopback bound is a property of the APP OBJECT, not of ``main()``.

The defect this guards is invisible to a test that only calls ``main()`` or only reads
``resolve_bind_host``: the Dockerfile CMD is ``uvicorn cdd_sow_research.api.app:app --host
0.0.0.0 --port ${PORT}``, so a bound that lives only in ``main()`` never runs in a shipped
process. Until this file existed, ``CDD_PROFILE=local`` served the seeded persona list whole,
subjects, tenants and group memberships included, to any peer that could reach the port, and
that peer could then act as the CDD approver by naming the persona in a header.

The second half is WHAT the guard is derived from. It must not be a service credential:
``CDD_S2S_TOKEN`` authenticates a calling SERVICE and no end user, so setting one is not
evidence that ``/v1/personas`` is protected; a guard derived from it switches OFF for exactly
the end-user routes it was protecting. The guard therefore reads the identity BINDING
(``ports/identity.py``), and the token cells below are the standing proof of that.

The third half, and the reason a rule keyed on the RUNTIME profile string would not have been
enough: this repo separates ``CDD_PROFILE`` from ``CDD_IDENTITY_PROFILE`` on purpose, so
``local`` can carry a verifying identity mode and a managed profile can carry the seeded
personas. The binding knows; neither name does.

The controls at the bottom keep the other cells from being true for a boring reason: a
VERIFYING binding must stand the guard DOWN, or "everything refuses" would just mean the
guard is stuck on.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cdd_sow_research.api.app import create_app
from cdd_sow_research.config import Settings

#: A peer on the LAN. RFC 5737 documentation address: no real host, and obviously fictional.
LAN_PEER = "192.0.2.50"

#: What a genuine dev run looks like to the guard.
LOOPBACK_PEER = "127.0.0.1"

_ENV = (
    "CDD_PROFILE",
    "CDD_IDENTITY_PROFILE",
    "CDD_CHANNEL_PROFILE",
    "CDD_S2S_TOKEN",
    "CDD_ALLOW_INSECURE_DEMO",
    "CDD_IAP_AUDIENCE",
)

# Every route the app serves that answers without a credential, including the two that need no
# identity at all: a deployment that can authenticate nobody has no business answering a
# stranger even about its own health.
UNCREDENTIALED_ROUTES = ("/healthz", "/v1/personas", "/.well-known/agent-card.json")


def _app_under(monkeypatch: pytest.MonkeyPatch, **env: str | None) -> Any:
    """Re-import the API module under a scrubbed environment and return its app object.

    The posture is resolved when the app is BUILT (the guard rides the app object), so an app
    built under the ambient environment would prove nothing about any other. Every variable
    this module reads is cleared first, so a cell that omits one is testing the absent state
    rather than inheriting the developer's shell.
    """
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        if value is not None:
            monkeypatch.setenv(name, value)

    from cdd_sow_research.api import deps

    deps.get_container.cache_clear()
    module = importlib.import_module("cdd_sow_research.api.app")
    return importlib.reload(module).app


def _status(app: Any, path: str, peer: str) -> int:
    with TestClient(app, client=(peer, 50000)) as client:
        return client.get(path, headers={"X-Dev-Persona": "approver"}).status_code


@pytest.mark.parametrize("path", UNCREDENTIALED_ROUTES)
@pytest.mark.parametrize(
    ("label", "env"),
    [
        # The cell this file was written for: an ordinary deployment shape, not a
        # misconfiguration. Setting a service credential must not unbound the end-user routes.
        (
            "local chosen, S2S token SET",
            {"CDD_PROFILE": "local", "CDD_S2S_TOKEN": "s3cret"},
        ),
        ("local chosen, no token", {"CDD_PROFILE": "local"}),
        # The identity mode names the seeded personas directly, whatever the runtime profile.
        (
            "identity mode local-persona named outright",
            {"CDD_PROFILE": "local", "CDD_IDENTITY_PROFILE": "local-persona"},
        ),
        # Unset is not consent: no profile and no identity mode means nothing was chosen.
        ("nothing chosen, token SET", {"CDD_S2S_TOKEN": "s3cret"}),
        # The on-premises placeholder resolves nobody until a client binds their own IdP.
        # A channel is named because this repo requires one for any mode it cannot infer, so
        # the cell is a VALID deployment the guard refuses rather than one the boot-time
        # validator rejects for an unrelated reason.
        (
            "onprem placeholder binding",
            {
                "CDD_PROFILE": "onprem",
                "CDD_IDENTITY_PROFILE": "onprem",
                "CDD_CHANNEL_PROFILE": "standalone",
            },
        ),
    ],
)
def test_a_posture_that_authenticates_no_end_user_refuses_a_lan_peer(
    monkeypatch: pytest.MonkeyPatch, label: str, env: dict[str, str], path: str
) -> None:
    app = _app_under(monkeypatch, **env)
    assert _status(app, path, LAN_PEER) == 503, f"{label}: {path} answered a LAN peer"


@pytest.mark.parametrize("path", UNCREDENTIALED_ROUTES)
def test_the_same_posture_still_serves_a_loopback_peer(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """The offline demo is the whole point of the local profile and must not regress."""
    app = _app_under(monkeypatch, CDD_PROFILE="local")
    assert _status(app, path, LOOPBACK_PEER) == 200


def test_the_local_profile_still_serves_its_seeded_personas_on_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal must be about the PEER, not about the personas having gone away."""
    app = _app_under(monkeypatch, CDD_PROFILE="local")
    with TestClient(app, client=(LOOPBACK_PEER, 50000)) as client:
        response = client.get("/v1/personas")
    assert response.status_code == 200
    assert [p["id"] for p in response.json()] == [
        "analyst",
        "approver",
        "auditor",
        "other-tenant",
    ]


def test_a_verifying_binding_stands_the_guard_down(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The control: without this, "everything refuses" would just mean the guard is stuck on.

    A fronted deployment (IAP verifies the assertion before the request arrives) must stay
    reachable and health-checkable off loopback. That it does NOT leak a seeded identity is
    the separate assertion below: ``/v1/personas`` is empty outside the persona binding.

    Built through ``create_app(settings)`` from a settings file naming ``iap``, the same way
    ``tests/unit/test_app_settings_posture.py`` builds a secure posture, so this exercises the
    factory path a fronted deployment actually uses.
    """
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CDD_IAP_AUDIENCE", "/projects/000/global/backendServices/000")
    monkeypatch.setenv("CDD_S2S_TOKEN", "s3cret")
    source = (
        Path("config/settings.yaml")
        .read_text()
        .replace("mode: ${CDD_IDENTITY_PROFILE:-}", "mode: iap", 1)
        .replace("mode: ${CDD_CHANNEL_PROFILE:-}", "mode: standalone", 1)
    )
    config_path = tmp_path / "secure-settings.yaml"
    config_path.write_text(source)
    settings = Settings.load(config_path)
    assert settings.identity_mode == "iap"

    app = create_app(settings)
    with TestClient(app, client=(LAN_PEER, 50000)) as client:
        assert client.get("/healthz").status_code == 200
        response = client.get("/v1/personas", headers={"X-Dev-Persona": "approver"})
    assert response.status_code == 200
    assert response.json() == [], "a verifying binding must publish no seeded identities"


def test_the_opt_out_is_the_documented_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """CDD_ALLOW_INSECURE_DEMO=1 is the single acknowledged exposure, as at bind time.

    An operator who deliberately demos off loopback keeps the escape hatch the bind guard has
    always honoured, so the guard adds no new way to get stuck.
    """
    app = _app_under(monkeypatch, CDD_PROFILE="local", CDD_ALLOW_INSECURE_DEMO="1")
    assert _status(app, "/v1/personas", LAN_PEER) == 200


def test_the_guard_is_not_derived_from_the_service_credential() -> None:
    """A drift guard: no service-credential variable may appear in the posture derivation.

    Asserting the BEHAVIOUR above is the real test; this asserts the SHAPE, so a future
    refactor that reintroduces a token into this function fails here with the reason spelled
    out rather than silently widening the exposure again.
    """
    import ast

    source = Path(__file__).resolve().parents[2] / "src" / "cdd_sow_research" / "api" / "app.py"
    text = source.read_text(encoding="utf-8")
    # Parsed rather than line-scanned: `ruff format` collapses or explodes this body depending
    # on how long the names are, and a guard anchored to a closing bracket in column 0
    # silently starts reading the wrong statement when that happens.
    tree = ast.parse(text)
    derivation = next(
        ast.get_source_segment(text, node) or ""
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_end_user_authenticated"
    )
    body = "\n".join(line for line in derivation.splitlines() if not line.lstrip().startswith("#"))
    assert "S2S_TOKEN" not in body.split('"""')[-1], (
        "the exposure guard is being derived from the service-to-service credential again; "
        "that secret authenticates a calling SERVICE and no end user, so it cannot speak for "
        "the end-user routes. Derive it from the identity binding (ports/identity.py)."
    )
