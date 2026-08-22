"""Browserless smoke test for the live SoW demo server (scripts/sow_demo_server.py).

Protects the flagship walkthrough from silent rot even where Chromium is not installed:
drives ``DemoSession`` through every scripted step and asserts each rendered page carries
the expected markers (coverage %, gap counts, the enhanced-diligence panels, the sealed
snapshot). The Playwright self-test (``make demo-selftest``) covers the browser layer; this
covers the server + renderer + engine wiring with only stdlib.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


@pytest.fixture(scope="module")
def demo_server():
    # scripts/ is not a package; load sow_demo_server (and its sibling imports) by path.
    sys.path.insert(0, str(_SCRIPTS))
    try:
        spec = importlib.util.spec_from_file_location(
            "sow_demo_server", _SCRIPTS / "sow_demo_server.py"
        )
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.path.remove(str(_SCRIPTS))


def test_walkthrough_drives_every_step_with_expected_state(demo_server) -> None:
    sess = demo_server.DemoSession()
    keys = [s["key"] for s in demo_server.STEPS]
    assert keys == [
        "opened",
        "round0",
        "round1",
        "round2",
        "related_parties",
        "screening",
        "scorecard",
        "source_of_funds",
        "monitoring",
        "approved",
    ]

    seen: list[dict] = []
    for _ in demo_server.STEPS:
        state = sess.state()
        html = sess.render()
        assert "democtl" in html  # control bar injected
        seen.append(state)
        if not sess.at_end:
            sess.advance()

    by_key = {s["key"]: s for s in seen}
    assert by_key["round0"]["coverage_pct"] == 69
    assert by_key["round0"]["gaps"] == 5
    assert by_key["round1"]["coverage_pct"] == 82
    assert by_key["round2"]["gaps"] == 0
    assert by_key["approved"]["final_status"] == "approved"

    final_html = sess.render()
    for panel in ("related_parties", "screening", "scorecard", "source_of_funds", "monitoring"):
        assert f'data-panel="{panel}"' in final_html


def test_goto_replays_deterministically(demo_server) -> None:
    sess = demo_server.DemoSession()
    sess.goto(6)  # scorecard
    assert sess.state()["key"] == "scorecard"
    # Replaying to the same step yields identical figures (pure/deterministic engine).
    cov_a = sess.state()["coverage_pct"]
    sess.goto(2)
    sess.goto(6)
    assert sess.state()["coverage_pct"] == cov_a
