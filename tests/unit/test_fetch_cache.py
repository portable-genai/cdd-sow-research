"""Unit tests for the demo-prep on-disk fetch cache (scripts/_fetch_cache.py).

The cache spares re-downloading the large, slow watchlist/EDGAR sources on every sync or
pack build. Loaded by path because it lives under scripts/, not the importable package.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "_fetch_cache.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("fetch_cache_under_test", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    monkeypatch.setenv("FETCH_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("FETCH_CACHE_TTL", raising=False)
    yield _load()


def test_a_fresh_copy_is_reused_without_refetching(cache: ModuleType) -> None:
    calls: list[str] = []

    def fetch(url: str) -> str:
        calls.append(url)
        return "PAYLOAD"

    first = cache.cached_fetch("https://example.test/list.xml", fetch)
    second = cache.cached_fetch("https://example.test/list.xml", fetch)

    assert first == second == "PAYLOAD"
    assert calls == ["https://example.test/list.xml"], "the second call must hit the cache"


def test_refresh_bypasses_the_cache(cache: ModuleType) -> None:
    calls: list[str] = []

    def fetch(url: str) -> str:
        calls.append(url)
        return f"v{len(calls)}"

    assert cache.cached_fetch("https://example.test/a", fetch) == "v1"
    assert cache.cached_fetch("https://example.test/a", fetch, refresh=True) == "v2"
    assert len(calls) == 2


def test_zero_ttl_disables_reuse(cache: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FETCH_CACHE_TTL", "0")
    calls: list[str] = []

    def fetch(url: str) -> str:
        calls.append(url)
        return "X"

    cache.cached_fetch("https://example.test/b", fetch)
    cache.cached_fetch("https://example.test/b", fetch)
    assert len(calls) == 2, "a zero TTL must never reuse a cached copy"


def test_an_expired_copy_is_refetched(cache: ModuleType, tmp_path: Path) -> None:
    calls: list[str] = []

    def fetch(url: str) -> str:
        calls.append(url)
        return "Y"

    cache.cached_fetch("https://example.test/c", fetch, ttl=10_000)
    # Age the cached file well past a short TTL, then read again.
    for path in (tmp_path / "cache").iterdir():
        os.utime(path, (0, 0))
    cache.cached_fetch("https://example.test/c", fetch, ttl=1)
    assert len(calls) == 2


def test_a_failed_fetch_is_not_cached(cache: ModuleType) -> None:
    def boom(url: str) -> str:
        raise RuntimeError("network down")

    with pytest.raises(RuntimeError):
        cache.cached_fetch("https://example.test/d", boom)

    # A later successful fetch must run (nothing poisoned the cache).
    assert cache.cached_fetch("https://example.test/d", lambda u: "ok") == "ok"
