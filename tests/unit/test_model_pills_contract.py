"""The half of the model-pill contract that lives in the BROWSER.

Every served console shows two small pills at the top right (owner decision, 2026-09-23): the
model that ANSWERED, and ``Search`` when an online search tool was used. They replaced the
full-width provenance banner, which named the model configuration would call rather than the one
that answered. The SERVICE half (the profile implies a runtime, ``/v1/healthz`` carries
``generator_model``, the route emits ``X-Answered-By`` / ``X-Search-Used``) is pinned in
``test_health_provenance.py`` and ``test_answer_provenance.py`` and is not restated here.

This file pins the other half, because that is the half that broke before. On 2026-09-04 eight
consoles were found to have rendered NOTHING on every page load since the banner landed: the
component called a path those trees did not serve, took the failure branch, and the failure
branch renders nothing, deliberately. A check that cannot fail loudly fails as an ABSENCE. The
pills can fail the same way and one more: a pill that renders but never moves, because the
fetch that carried the answer went around the wrapper that reads it.

This console's shape: the pills live in ``ui/components/ModelPills.tsx`` and are mounted by the
app chrome (``AppChrome``), which the root layout wraps around every page. Every call reaches the
service through ``lib/api`` at ``API_BASE`` (``/agent/api``), which the Next rewrite proxies with
every response header intact, so there is no route handler to forward them.
"""

from __future__ import annotations

import re
from pathlib import Path

UI = Path("ui")
PILLS = UI / "components" / "ModelPills.tsx"


def _code_only(source: str) -> str:
    """``source`` with ``//`` line comments and ``/* */`` blocks removed, so prose cannot pass."""
    return re.sub(r"/\*.*?\*/|//[^\n]*", "", source, flags=re.S)


def test_the_pills_are_mounted_on_every_page_by_the_chrome() -> None:
    """Mounted per page, they are present on the pages somebody remembered and absent on the one
    a screenshot came from. The chrome is mounted by the root layout, which no route can skip."""
    layout = (UI / "app" / "layout.tsx").read_text()
    assert "<AppChrome>" in layout, "the root layout no longer wraps every page in the chrome"
    chrome = _code_only((UI / "components" / "AppChrome.tsx").read_text())
    # Both branches: the standalone console and the embedded one.
    assert chrome.count("<ModelPills />") == 2, "the pills are missing from a chrome branch"


def test_the_pills_start_from_healthz_through_the_consoles_own_client() -> None:
    """A health call on a base of its own is how the eight silent banners happened."""
    pills = _code_only(PILLS.read_text())
    assert re.search(r'import \{[^}]*\bhealth\b[^}]*\} from "\.\./lib/api"', pills)
    assert "health()" in pills
    assert "generator_model" in pills and "runtime" in pills
    assert "running on GCP" in pills and "running locally" in pills
    assert "answered the last request" in pills
    api = (UI / "lib" / "api.ts").read_text()
    assert 'request("/v1/healthz")' in api, "health no longer reads the service's /v1/healthz"


def test_the_pills_read_both_headers_off_the_base_every_call_uses() -> None:
    pills = _code_only(PILLS.read_text())
    assert "watchAnswers(window, API_BASE," in pills, (
        "the answers must be read off the same base the console calls, or a pill never moves"
    )
    watcher = (UI / "lib" / "answer-provenance.mjs").read_text()
    for header in ('"x-answered-by"', '"x-search-used"'):
        assert header in watcher, "the pills never read " + header
    assert (UI / "tests" / "answer-provenance.test.mjs").exists()


def test_the_transport_goes_through_the_wrapper_rather_than_around_it() -> None:
    """The defect this console could ship with every other assertion green.

    ``AuthenticatedTransport`` defaulted its ``fetch`` to the global captured when the module
    loaded, before the pills installed their wrapper on mount, so every dossier request would
    have bypassed it and the pill would have stayed on the configured model forever.
    """
    transport = _code_only((UI / "lib" / "embed" / "transport.ts").read_text())
    assert "fetchImpl: typeof fetch = fetch" not in transport
    assert "this.fetchImpl ?? globalThis.fetch" in transport
    assert "this.fetchImpl.call(" not in transport, "a call site still uses the captured fetch"


def test_the_rewrite_proxies_the_api_with_its_headers() -> None:
    """A rewrite forwards the upstream response whole; a route handler would have to copy them.

    If this console ever swaps the rewrite for a handler under ``ui/app/api``, that handler must
    forward both answer headers, and this assertion is the reminder.
    """
    config = (UI / "next.config.mjs").read_text()
    assert 'source: "/api/:path*"' in config
    handlers = [p for p in (UI / "app").rglob("route.ts") if "api" in p.parts]
    for handler in handlers:
        source = handler.read_text()
        for header in ("x-answered-by", "x-search-used"):
            assert header in source, f"{handler} proxies the API and drops {header}"


def test_the_pills_are_pinned_where_a_reader_can_see_them() -> None:
    """Fixed to the viewport's top right, so no page geometry can push them out of view.

    The old strip's failure mode was a negative margin hoisting it above the viewport. The pills
    carry no offset of their own to get wrong, and the full-width strip must not come back.
    """
    pills = _code_only(PILLS.read_text())
    container = re.search(r'className="(fixed [^"]*)"', pills)
    assert container, "the pills are not position: fixed"
    classes = container.group(1).split()
    assert any(c.startswith("top-") for c in classes) and any(
        c.startswith("right-") for c in classes
    )
    assert not any(c.startswith("-") for c in classes), "a negative offset can hide the pills"
    chrome = (UI / "components" / "AppChrome.tsx").read_text()
    assert "running on GCP" not in chrome, "the old banner sentence is back in the chrome"
