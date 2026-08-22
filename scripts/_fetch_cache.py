"""On-disk cache for the large, slow, repeatedly-downloaded source files the demo-prep
scripts pull (OFAC + UN watchlists in ``sync_sanctions.py``, SEC EDGAR facts in
``build_demo_pack.py``). A cached copy younger than the TTL is reused, so re-running a
sync or a pack build does not re-download tens of megabytes each time.

The cache lives under ``scripts/out/cache`` (gitignored) and keys each entry by a hash of
its URL. Controls:

  - ``refresh=True`` (or the scripts' ``--refresh`` flag) bypasses the cache and refetches.
  - ``FETCH_CACHE_TTL`` (seconds) overrides the 24h default; ``0`` disables reuse.
  - ``FETCH_CACHE_DIR`` overrides the cache location (used by the tests).

Text-only by design: the publishers here return text (CSV / XML / JSON), which the callers
parse. Cache hits and misses are announced on stderr so a run is never silently stale.
"""

from __future__ import annotations

import hashlib
import sys
import time
from collections.abc import Callable
from pathlib import Path

from cdd_sow_research.envread import optional_setting

_DEFAULT_TTL_SECONDS = 24 * 3600
_DEFAULT_CACHE_DIR = Path(__file__).resolve().parent / "out" / "cache"


def cache_dir() -> Path:
    override = optional_setting("FETCH_CACHE_DIR") or ""
    return Path(override) if override else _DEFAULT_CACHE_DIR


def ttl_seconds() -> float:
    raw = optional_setting("FETCH_CACHE_TTL")
    if raw is None:
        return float(_DEFAULT_TTL_SECONDS)
    try:
        return max(0.0, float(raw))
    except ValueError as exc:
        raise ValueError("FETCH_CACHE_TTL must be a non-negative number of seconds") from exc


def _path_for(url: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    # A readable hint of the source keeps the cache dir browsable; the digest keeps it unique.
    hint = "".join(c if c.isalnum() else "-" for c in url.split("://", 1)[-1])[:48].strip("-")
    return cache_dir() / f"{hint}-{digest}.cache"


def cached_fetch(
    url: str,
    fetch: Callable[[str], str],
    *,
    refresh: bool = False,
    ttl: float | None = None,
) -> str:
    """Return the text for ``url`` from disk when a fresh copy exists, else via ``fetch``.

    A successful fetch is written to the cache (atomically) for the next run. A fetch
    failure is never cached, so a bad run does not poison later ones.
    """
    path = _path_for(url)
    max_age = ttl_seconds() if ttl is None else ttl
    if not refresh and max_age > 0 and path.is_file():
        age = time.time() - path.stat().st_mtime
        if age <= max_age:
            print(f"cache hit {url} (age {int(age)}s)", file=sys.stderr)
            return path.read_text(encoding="utf-8")

    text = fetch(url)

    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return text
