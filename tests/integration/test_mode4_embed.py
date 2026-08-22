"""Mode 4 authentication reaches the FastAPI request context end to end.

Guarded by ``pytest.importorskip("jwt")``: the shared helpers come from
``tests.unit.test_access_token_identity``, which needs the optional ``oidc`` extra. The
guard keeps the SDK-free ``[dev]``-only gate collecting this module rather than erroring
on it, even though the ``integration`` marker deselects it there.
"""

from __future__ import annotations

import pytest

pytest.importorskip("jwt")

import respx  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from tests.unit.test_access_token_identity import (  # noqa: E402
    _AGENT_ORIGIN,
    _INSTALLATION,
    _JWKS,
    _rsa_key,
    _settings,
    _token,
)

from cdd_sow_research.adapters.oidc import jwks_verify  # noqa: E402
from cdd_sow_research.api import deps  # noqa: E402
from cdd_sow_research.api.security import CurrentAuthenticatedContext  # noqa: E402
from cdd_sow_research.config import Container  # noqa: E402

pytestmark = pytest.mark.integration


def test_mode4_bearer_reaches_normalized_authenticated_context(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jwks_verify._cache.clear()
    key, jwk, algorithm = _rsa_key()
    settings = _settings(tmp_path, algorithm=algorithm)
    container = Container(settings)
    monkeypatch.setattr(deps, "get_container", lambda: container)
    app = FastAPI()

    @app.post("/protected")
    def protected(context: CurrentAuthenticatedContext) -> dict[str, object]:
        return {
            "actor": context.principal.actor,
            "tenant": context.principal.tenant,
            "token_type": context.evidence.token_type,
            "installation": context.evidence.installation,
            "scopes": context.evidence.effective_scopes,
            "correlation": context.evidence.correlation,
        }

    token = _token(key, algorithm, jwk["kid"])
    with respx.mock:
        respx.get(_JWKS).respond(json={"keys": [jwk]})
        response = TestClient(app, client=("127.0.0.1", 50000)).post(
            "/protected",
            headers={
                "Authorization": f"Bearer {token}",
                "X-CDD-Installation-ID": _INSTALLATION,
                "Origin": _AGENT_ORIGIN,
            },
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tenant"] == "demo-bank"
    assert body["token_type"] == "at+jwt"
    assert body["installation"] == _INSTALLATION
    assert body["scopes"] == ["cdd.read", "cdd.write"]
    assert len(body["correlation"]) == 64
    assert token not in response.text
