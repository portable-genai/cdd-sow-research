from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/laptop_demo_playwright.py"
SPEC = importlib.util.spec_from_file_location("laptop_demo_playwright", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
DEMO = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = DEMO
SPEC.loader.exec_module(DEMO)


def test_step_selection_is_resumable_and_rejects_unknown_ids() -> None:
    assert [step.id for step in DEMO.selected_steps("dossier")] == [
        "dossier",
        "portable-record",
    ]
    with pytest.raises(ValueError, match="unknown step"):
        DEMO.selected_steps("not-a-step")


def test_list_mode_does_not_start_browser_or_services(capsys) -> None:
    assert DEMO.main(["--list"]) == 0
    output = capsys.readouterr().out
    assert "capabilities" in output
    assert "portable-record" in output
