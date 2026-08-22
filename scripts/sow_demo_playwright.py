"""Presenter-controlled Playwright walkthrough of the live SoW demo.

Drives a headed browser through the full Source-of-Wealth flow served by
``scripts/sow_demo_server.py``. It is **paced by the presenter** and narrates entirely on
this terminal (the *Playwright console*), NEVER in the browser, so the audience sees only
the clean application UI while you read the narration aloud. Before each step it prints
what is about to happen and waits for you; then it performs the action, verifies the page
actually reached the expected state, and scrolls the relevant panel into view.

One command, one terminal: if the demo server is not already running this script starts it
itself (and stops it on exit). Point ``DEMO_URL`` at an already-running server to attach
instead.

    pip install -e ".[demo]" && playwright install chromium     # one-time
    python scripts/sow_demo_playwright.py

At each prompt: press Enter to run the step, ``b`` to go back one step, a step number
(``1``-``10``) to jump, or ``q`` to quit.

Environment overrides:
    DEMO_URL     attach to this already-running server instead of spawning one
    DEMO_PORT    port for the auto-spawned server (default 8099)
    HEADLESS=1   run headless (used for the self-test; no window)
    DEMO_AUTO=1  don't wait for input, advance automatically (self-test / recording)
    SLOWMO_MS    per-action slow-motion in ms (default 250 headed, 0 headless)
    CHROME_PATH  explicit Chromium/Chrome binary (else Playwright's own)
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

from cdd_sow_research.envread import setting_or_default

_PORT = setting_or_default("DEMO_PORT", "8099")
BASE = os.environ.get("DEMO_URL", f"http://127.0.0.1:{_PORT}")
HEADLESS = os.environ.get("HEADLESS") == "1"
AUTO = os.environ.get("DEMO_AUTO") == "1"
SLOWMO = int(os.environ.get("SLOWMO_MS", "0" if HEADLESS else "250"))
CHROME_PATH = os.environ.get("CHROME_PATH") or None

# ANSI colours for the presenter console (no external dependency). Disabled when the
# output is redirected (e.g. CI logs) so captured output stays clean.
_TTY = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text


# One entry per server step (must match sow_demo_server.STEPS in order). Each row:
#   key        the server step key (asserted against /state so the two never drift)
#   say        the narration the presenter reads aloud
#   expect     an optional dict of /state fields to assert after the step runs
#   panel      the data-panel to scroll into view + highlight (None = top of page)
STEPS = [
    {
        "key": "opened",
        "say": "Case opened. The client's self-declaration is captured: declared net worth "
        "USD 60m-100m across four wealth sources, but NO evidence on file yet.",
        "expect": {},
        "panel": None,
    },
    {
        "key": "round0",
        "say": "Round 0: the RM ingests the first document pack. Coverage jumps to ~69%, "
        "FIVE gaps appear (uncorroborated sources, a missing registry extract, a stale "
        "statement, an unreconciled total), and the system drafts client RFIs.",
        "expect": {"coverage_pct": 69, "gaps": 5},
        "panel": "gaps",
    },
    {
        "key": "round1",
        "say": "Round 1: weeks later the client responds with the ACRA extract, an employment "
        "letter and the grant of probate. Coverage rises to ~82%; gaps drop to 2.",
        "expect": {"coverage_pct": 82, "gaps": 2},
        "panel": "gaps",
    },
    {
        "key": "round2",
        "say": "Round 2: the client sends a fresh 2026 brokerage statement. Coverage ~86% and "
        "every source is corroborated, so the gaps panel goes green: ready for review.",
        "expect": {"coverage_pct": 86, "gaps": 0},
        "panel": "reconciliation",
    },
    {
        "key": "related_parties",
        "say": "Because Acme is a company, onboarding it also runs CDD + a SoW sub-case on its "
        "key individuals (UBOs >=25% and directors). One UBO is a PEP, which softly "
        "escalates the parent to enhanced review (a checker still disposes).",
        "expect": {},
        "panel": "related_parties",
    },
    {
        "key": "screening",
        "say": "Every in-scope party is screened against the sanctions / PEP / watchlist "
        "snapshot. The PEP UBO matches OFAC SDN: the hit becomes an alert an analyst "
        "dispositions. Each alert names the exact list version it matched.",
        "expect": {},
        "panel": "screening",
    },
    {
        "key": "scorecard",
        "say": "The deterministic risk scorecard runs across customer type, geography, "
        "product, channel, PEP and adverse media. Hard signals (the open sanctions hit "
        "and the PEP) force EDD and raise the band: Acme lands PROHIBITED / EDD.",
        "expect": {},
        "panel": "scorecard",
    },
    {
        "key": "source_of_funds",
        "say": "Source of Funds (distinct from Source of Wealth) reconciles the declared "
        "expected inflows against the evidenced credit advices: ~60% coverage with two "
        "HIGH gaps - a declared asset-sale with no advice, and an undeclared gift inflow.",
        "expect": {},
        "panel": "source_of_funds",
    },
    {
        "key": "monitoring",
        "say": "Ongoing monitoring sets a risk-based review cadence (EDD = annual) and derives "
        "event triggers. The relationship is OVERDUE with four ranked re-review triggers "
        "(overdue review, open sanctions hit, unusual SoF activity, PEP exposure).",
        "expect": {},
        "panel": "monitoring",
    },
    {
        "key": "approved",
        "say": "Maker-checker: the MLRO approves under four-eyes (the checker is not the maker) "
        "and an immutable, hash-chained snapshot is sealed. Case complete.",
        "expect": {"final_status": "approved"},
        "panel": None,
    },
]


def _get_state() -> dict | None:
    try:
        with urllib.request.urlopen(BASE + "/state", timeout=2) as resp:
            return json.load(resp)
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _reachable() -> bool:
    return _get_state() is not None


def _spawn_server() -> subprocess.Popen | None:
    """Start the demo server ourselves so the whole demo is one command."""
    server = Path(__file__).with_name("sow_demo_server.py")
    env = {**os.environ, "PYTHONPATH": "src"}
    print(_c("2", f"Starting demo server on port {_PORT} ..."))
    proc = subprocess.Popen(
        [sys.executable, str(server), "--port", _PORT],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(50):  # up to ~5s
        if _reachable():
            return proc
        if proc.poll() is not None:  # died on startup
            return None
        time.sleep(0.1)
    proc.terminate()
    return None


def _prompt(idx: int) -> str:
    """Return 'next' | 'back' | 'quit' | 'goto:N'. Auto-advances under DEMO_AUTO."""
    if AUTO:
        time.sleep(0.8)
        return "next"
    try:
        raw = (
            input(_c("2", "        ⏎ next  ·  b back  ·  1-10 jump  ·  q quit  ›  "))
            .strip()
            .lower()
        )
    except EOFError:
        return "next"
    if raw in ("q", "quit"):
        return "quit"
    if raw in ("b", "back"):
        return "back"
    if raw.isdigit():
        return f"goto:{max(1, min(int(raw), len(STEPS))) - 1}"
    return "next"


def _spotlight(page, panel: str | None) -> None:
    """Scroll the step's panel into view and flash a highlight (cosmetic only)."""
    if not panel:
        page.evaluate("window.scrollTo({top: 0, behavior: 'smooth'})")
        return
    with contextlib.suppress(Exception):
        page.eval_on_selector(
            f'[data-panel="{panel}"]',
            "el => { el.scrollIntoView({behavior:'smooth', block:'center'});"
            " el.style.transition='box-shadow .3s';"
            " el.style.boxShadow='0 0 0 3px #3a60f0';"
            " setTimeout(()=>el.style.boxShadow='', 1800); }",
        )


def _assert_state(step: dict, state: dict | None) -> list[str]:
    """Verify the live page reached the expected step + figures; return any problems."""
    problems: list[str] = []
    if state is None:
        return [f"could not read /state for step {step['key']!r}"]
    if state.get("key") != step["key"]:
        problems.append(f"expected step key {step['key']!r}, server is at {state.get('key')!r}")
    for field, want in step["expect"].items():
        got = state.get(field)
        if got != want:
            problems.append(f"{field}: expected {want!r}, got {got!r}")
    return problems


def _go(page, target_idx: int) -> None:
    """Drive the server to ``target_idx`` (advance one step, or /goto for back/jump)."""
    state = _get_state() or {"step": 0}
    cur = state["step"]
    if target_idx == cur + 1:
        btn = page.locator(".democtl button.next")
        if btn.count() and btn.is_enabled():
            btn.click()
            page.wait_for_load_state("load")
            return
    page.goto(f"{BASE}/goto?step={target_idx}", wait_until="load")


def main() -> int:
    spawned: subprocess.Popen | None = None
    if not _reachable():
        if os.environ.get("DEMO_URL"):
            print(_c("31", f"Cannot reach the demo server at {BASE} (DEMO_URL is set)."))
            return 2
        spawned = _spawn_server()
        if spawned is None:
            print(_c("31", "Failed to start the demo server."))
            print("Start it manually: PYTHONPATH=src python scripts/sow_demo_server.py")
            return 2

    exit_code = 0
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=HEADLESS, slow_mo=SLOWMO, executable_path=CHROME_PATH
            )
            page = browser.new_context(viewport={"width": 1200, "height": 900}).new_page()

            print()
            print(_c("1;36", "  Source-of-Wealth live demo — presenter-controlled walkthrough"))
            print(
                _c("2", "  Narration prints here (read it aloud); the browser shows only the UI.\n")
            )
            for i, step in enumerate(STEPS):
                pad = f"{i + 1}/{len(STEPS)}"
                mark = _c("2", "•") if step["expect"] == {} else _c("32", "✓")
                print(f"  {_c('1;36', pad):>10}  {mark}  {step['say']}")

            _go(page, 0)  # always start clean; loop invariant: server is displaying step `idx`

            idx = 0
            while idx < len(STEPS):
                step = STEPS[idx]
                print()
                print(_c("1;36", f"  [{idx + 1}/{len(STEPS)}] {step['key']}"))
                print("  " + step["say"])

                # Verify the page actually reached this step, and spotlight its panel.
                page.wait_for_timeout(150)
                problems = _assert_state(step, _get_state())
                if problems:
                    exit_code = 1
                    for prob in problems:
                        print(_c("31", f"        ✗ {prob}"))
                else:
                    print(_c("32", "        ✓ page verified"))
                _spotlight(page, step["panel"])

                action = _prompt(idx)
                if action == "quit":
                    print(_c("2", "  quitting."))
                    break
                if action == "back":
                    idx = max(0, idx - 1)
                elif action.startswith("goto:"):
                    idx = int(action.split(":", 1)[1])
                elif idx < len(STEPS) - 1:  # next
                    idx += 1
                else:
                    break  # 'next' on the last step ends the walkthrough
                _go(page, idx)

            # Finish on the timeline recap.
            page.goto(BASE + "/timeline", wait_until="load")
            print()
            print(_c("1;36", "  Timeline recap shown. The browser stays open for questions."))
            if not AUTO:
                with contextlib.suppress(EOFError):
                    input(_c("2", "        ⏎ press Enter to close the browser… "))
            browser.close()
    finally:
        if spawned is not None:
            spawned.terminate()
            with contextlib.suppress(Exception):
                spawned.wait(timeout=5)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
