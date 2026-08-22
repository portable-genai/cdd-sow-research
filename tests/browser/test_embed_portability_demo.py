"""Unit coverage for the presenter-paced portability evidence runner."""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture(scope="module")
def runner() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "scripts" / "embed_portability_demo.py"
    spec = importlib.util.spec_from_file_location("embed_portability_demo_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_step_selection_is_inclusive_and_unknown_ids_fail(runner: ModuleType) -> None:
    selected = runner.selected_steps("same-artifact")
    assert [step.id for step in selected] == [
        "same-artifact",
        "handshake-boundary",
        "fallback",
    ]
    with pytest.raises(ValueError, match="unknown step"):
        runner.selected_steps("missing")


def test_list_needs_no_browser_or_services(
    runner: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    assert runner.main(["--list", "--scope", "full", "--from", "fallback"]) == 0
    output = capsys.readouterr().out
    assert "fallback: Deny an unregistered host" in output
    assert "identity-mode4: Verify direct institutional access-token identity" in output
    assert "identity-mode5: Verify the BFF-authorized embedded identity grant" in output
    assert "PRESENTER NOTES" not in output


class _Page:
    def __init__(self) -> None:
        self.closed = False

    def set_default_timeout(self, timeout: int) -> None:
        assert timeout == 20_000

    def screenshot(self, **_kwargs: object) -> None:
        return

    def close(self) -> None:
        self.closed = True


class _Context:
    def __init__(self) -> None:
        self.closed = False

    def new_page(self) -> _Page:
        return _Page()

    def close(self) -> None:
        self.closed = True


class _Browser:
    def __init__(self, context: _Context) -> None:
        self.context = context
        self.closed = False

    def new_context(self, **_kwargs: object) -> _Context:
        return self.context

    def close(self) -> None:
        self.closed = True


def test_no_pause_never_reads_stdin_and_cleanup_always_runs(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _Context()
    browser = _Browser(context)
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch=lambda **_kwargs: browser))
    monkeypatch.setattr(runner, "loader_evidence", lambda: ("sha384-proof", "sha256-proof"))
    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt: (_ for _ in ()).throw(AssertionError("stdin was read")),
    )
    called: list[str] = []
    step = runner.Step(
        id="proof",
        title="Proof",
        presenter_notes="The reviewer opens the proof. The system shows the result.",
        action=lambda _page, _evidence: called.append("action"),
    )

    evidence = runner.run_browser(
        playwright,
        "chromium",
        (step,),
        headless=True,
        slow_mo=0,
        pause=False,
        screenshots=None,
    )

    assert called == ["action"]
    assert evidence.completed_steps == ["proof"]
    assert context.closed is True
    assert browser.closed is True


def test_cleanup_runs_when_a_step_fails(
    runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _Context()
    browser = _Browser(context)
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch=lambda **_kwargs: browser))
    monkeypatch.setattr(runner, "loader_evidence", lambda: ("sha384-proof", "sha256-proof"))
    step = runner.Step(
        id="broken",
        title="Broken",
        presenter_notes="The reviewer opens the proof. The system reports a failure.",
        action=lambda _page, _evidence: (_ for _ in ()).throw(AssertionError("broken proof")),
    )

    with pytest.raises(AssertionError, match="broken proof"):
        runner.run_browser(
            playwright,
            "chromium",
            (step,),
            headless=True,
            slow_mo=0,
            pause=False,
            screenshots=None,
        )

    assert context.closed is True
    assert browser.closed is True


def test_presenter_notes_are_spoken_business_narration(runner: ModuleType) -> None:
    forbidden = ("localhost", "synthetic", "iframe", "selector", "click here")
    for step in (*runner.STEPS, *runner.IDENTITY_STEPS):
        assert 35 <= len(step.presenter_notes.split()) <= 75
        assert 2 <= step.presenter_notes.count(".") <= 4
        assert all(term not in step.presenter_notes.lower() for term in forbidden)


def test_channel_scope_marks_identity_as_not_run(runner: ModuleType) -> None:
    assert runner.channel_dependency_status() == {
        "identity.mode4": {
            "status": "NOT_RUN",
            "reason": "run --scope full for production-backed browser identity evidence",
        },
        "identity.mode5": {
            "status": "NOT_RUN",
            "reason": "run --scope full for production-backed browser identity evidence",
        },
    }


def test_full_scope_uses_built_in_production_backed_identity_evidence(
    runner: ModuleType,
) -> None:
    harness = SimpleNamespace(
        mode4_evidence=lambda: {"status": "ready", "dimension": "identity.mode4"},
        mode5_evidence=lambda: {"status": "not_ready", "dimension": "identity.mode5"},
    )

    dependencies = runner.identity_dependency_status(harness)

    assert dependencies["identity.mode4"]["status"] == "PASS"
    assert dependencies["identity.mode5"]["status"] == "FAIL"
    assert dependencies["identity.mode4"]["evidence"]["dimension"] == "identity.mode4"


def test_boundary_gate_fails_closed_without_expected_traffic(runner: ModuleType) -> None:
    with pytest.raises(runner.DimensionFailure, match="observed no required traffic"):
        runner._assert_required_boundary_traffic(
            {"counters": {}, "events": []},
            (("message-port", "host-to-agent", 1),),
            dimension="identity.mode5",
        )


def test_mutation_sentinel_under_renamed_message_still_fails_gate(
    runner: ModuleType,
) -> None:
    class MutationPage:
        def __init__(self) -> None:
            self.sent = ""

        def evaluate(self, script: str, value: str | None = None) -> object:
            if "completely_renamed_payload" in script:
                assert value is not None
                self.sent = value
                return None
            assert "__cddHostBoundaryProbe" in script
            digest = hashlib.sha256(self.sent.encode()).hexdigest()
            return {
                "counters": {"message-port:host-to-agent": 1},
                "events": [
                    {
                        "surface": "message-port",
                        "direction": "host-to-agent",
                        "digests": [digest],
                        "observation_failed": False,
                    }
                ],
            }

    page = MutationPage()

    assert runner._prove_boundary_mutation_gate(page) == "PASS"
    assert page.sent == "FORBIDDEN-BOUNDARY-MUTATION-SENTINEL"
