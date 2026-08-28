"""Serving A2A needs the SDK's server extra, and the bare pin does not declare it.

`a2a-sdk` moved from `>=0.2` to `>=1.1` in the 2026-08-26 currency sweep, across a major version
boundary. The floor lives in the `[gcp]` extra and every import of it is lazy, so the offline
gate never installs it and could not have proved anything about it. The bump was recorded at the
time as unverified. Verifying it found this.

**1.x moved `starlette` and `sse-starlette` out of the base package into extras**, so a bare
`a2a-sdk>=1.1` declares a CLIENT-ONLY SDK. `to_a2a` still imports, which is what makes it quiet:
the failure arrives at ASGI startup, as `ModuleNotFoundError: No module named 'sse_starlette'`
raised from `a2a.server.routes`, at the moment the agent would begin serving. Proved by
installing `google-adk==2.7.1` with a bare `a2a-sdk>=1.1` and starting the app; with
`a2a-sdk[http-server]>=1.1` the same app serves `/.well-known/agent-card.json` with a 200.

**The more useful half of the finding is why nothing had broken yet.** `requirements-gcp.lock`
already resolves `sse-starlette`, so an install from the lock serves A2A correctly today. It is
there `via mcp`, not via `a2a-sdk`: the MCP SDK is a separate pin in the same extra that happens
to need the same package. So serving worked by COINCIDENCE of an unrelated dependency rather
than by anything this tree declared, and it would have broken the day `mcp` dropped it or a
profile installed A2A without MCP. An install from `pyproject` rather than the lock is broken
today.

Declaring the extra makes the dependency real rather than borrowed. This guard is static because
it has to be: an offline gate cannot install the extra, and the thing worth pinning is the
declared dependency rather than whatever a venv happens to contain.
"""

from __future__ import annotations

import pathlib
import re

import pytest

PYPROJECT = pathlib.Path(__file__).resolve().parents[2] / "pyproject.toml"


def _a2a_requirements() -> list[str]:
    text = PYPROJECT.read_text()
    return re.findall(r'"(a2a-sdk[^"]*)"', text)


def test_the_pin_exists_at_all() -> None:
    assert _a2a_requirements(), "this tree serves A2A, so it must declare a2a-sdk"


@pytest.mark.parametrize("requirement", _a2a_requirements())
def test_the_a2a_pin_carries_the_server_extra(requirement: str) -> None:
    """A tree that calls `to_a2a` serves A2A, so it needs the server half of the SDK."""
    assert "[" in requirement and "http-server" in requirement, (
        f"{requirement!r} installs a client-only SDK; serving needs a2a-sdk[http-server]"
    )
