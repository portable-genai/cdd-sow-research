"""The service configures the fleet's logging formatter, in the process that actually ships.

This exists because the deployed service did not. Read live against the reference deployment
on 2026-09-15: of roughly 60 `severity>=ERROR` entries on `cloud_run_revision` in thirty days,
every one came from the platform's own request log or from raw stderr. Python tracebacks
arrived as multi-line `textPayload` fragments split across several entries, with no `severity`
from the application, no logger name and no trace field. `configure_logging` was called in 75
files across the workspace and in none of the seven deployed stacks.

What that cost is specific rather than cosmetic. Cloud Logging reads `severity` from the
payload, so an application error was indistinguishable from an info line and could not drive a
log-based metric. `logging.googleapis.com/trace` is what puts a log line inside the request it
came from. And a traceback split across N entries has no single entry carrying the exception,
which is what Error Reporting groups on.

The assertions are about the SHIPPED shape rather than about the kit, which has its own tests.
"""

from __future__ import annotations

import importlib
import json
import logging
import sys
from typing import Any

import pytest
from hex_service_kit.logging import reset_logging_for_tests

_PROFILE_ENV = "CDD_PROFILE"
_SERVED_MODULE = "cdd_sow_research.api.app"
_SERVICE = "cdd-sow-research"


@pytest.fixture(autouse=True)
def _clean_logging() -> Any:
    """Each test owns the root logger, and hands it back."""
    reset_logging_for_tests()
    root = logging.getLogger()
    handlers = root.handlers[:]
    level = root.level
    yield
    reset_logging_for_tests()
    root.handlers[:] = handlers
    root.setLevel(level)


def _reimport(monkeypatch: pytest.MonkeyPatch, profile: str) -> None:
    """Import the API module the way a shipped process does: at module scope."""
    monkeypatch.setenv(_PROFILE_ENV, profile)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "demo-project")
    sys.modules.pop(_SERVED_MODULE, None)
    importlib.import_module(_SERVED_MODULE)


def test_importing_the_served_module_configures_logging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Dockerfile CMD serves the app OBJECT, so import must be enough.

    Proved red before it was trusted: with the module-scope `configure_logging` call removed,
    the root logger carries whatever the test runner left on it and this fails.
    """
    _reimport(monkeypatch, "gcp")
    root = logging.getLogger()
    assert len(root.handlers) == 1, "the kit installs exactly one handler"
    assert type(root.handlers[0].formatter).__name__ == "CloudLoggingFormatter"


def test_a_cloud_profile_error_is_one_json_object_the_platform_can_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The field names are load-bearing, so they are named rather than counted."""
    _reimport(monkeypatch, "gcp")
    formatter = logging.getLogger().handlers[0].formatter
    assert formatter is not None

    try:
        raise ValueError("downstream dependency refused the write")
    except ValueError:
        record = logging.LogRecord(
            name=f"{_SERVICE}.adapter",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="request failed",
            args=(),
            exc_info=sys.exc_info(),
        )

    payload = json.loads(formatter.format(record))
    assert payload["severity"] == "ERROR"
    assert payload["service"] == _SERVICE
    # One entry carries the whole traceback, which is what Error Reporting groups on. A
    # traceback split across entries, which is what the deployment emitted, groups as nothing.
    assert "request failed" in payload["message"]
    assert "Traceback (most recent call last)" in payload["message"]
    assert "ValueError: downstream dependency refused the write" in payload["message"]


def test_the_offline_profile_stays_readable_at_a_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`local` is a person at a terminal running a demo, so it is text and not JSON."""
    _reimport(monkeypatch, "local")
    formatter = logging.getLogger().handlers[0].formatter
    assert type(formatter).__name__ == "Formatter"


def test_the_installed_cli_entry_point_configures_logging_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`[project.scripts]` names `main:app`, so a module-guard call would run for nobody.

    The regression guard for a real mistake made while writing this change: the call was first
    placed under `if __name__ == "__main__"`, which never executes for the installed console
    script. It is a Typer callback instead, asserted as a registered callback rather than as
    source text.
    """
    cli_main = importlib.import_module("cdd_sow_research.cli.main")

    assert cli_main.app.registered_callback is not None
    monkeypatch.setenv(_PROFILE_ENV, "gcp")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "demo-project")
    cli_main.app.registered_callback.callback()
    assert type(logging.getLogger().handlers[0].formatter).__name__ == "CloudLoggingFormatter"


def test_the_cli_callback_never_pre_empts_a_profile_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected profile stays the command's error to report, not the callback's.

    Observed failing first, in `agent-observability`, where resolving the profile inside the
    callback raised before the command path could turn a mis-capitalised value into an
    operator-readable exit, replacing that sentence with a traceback.
    """
    cli_main = importlib.import_module("cdd_sow_research.cli.main")

    monkeypatch.setenv(_PROFILE_ENV, "NotAProfile")
    cli_main.app.registered_callback.callback()  # must not raise
    assert logging.getLogger().handlers, "logging is still configured on a bad profile"
