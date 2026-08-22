"""Canonical public Mode 6 auth-route proxy fixture."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def proxy_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "embed_dev_proxy.py"
    spec = importlib.util.spec_from_file_location("embed_dev_proxy_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("public", "internal"),
    [
        ("/agent/auth/login", "/auth/login"),
        (
            "/agent/auth/callback?code=abc&state=xyz",
            "/auth/callback?code=abc&state=xyz",
        ),
        ("/agent/auth/logout", "/auth/logout"),
    ],
)
def test_public_auth_prefix_is_stripped_once(proxy_module, public: str, internal: str) -> None:
    assert proxy_module.internal_auth_target(public) == internal


@pytest.mark.parametrize(
    "target",
    ["/auth/login", "/agent/api/v1/cdd", "/agent/authentic"],
)
def test_proxy_rejects_every_non_auth_target(proxy_module, target: str) -> None:
    with pytest.raises(ValueError, match="outside"):
        proxy_module.internal_auth_target(target)
