"""WP3: the eval smoke/promotion split (eval/run_eval.py --mode).

smoke (default) is the offline pre-merge check; gate is the model-quality-gate promotion authority
and must refuse to run under local/onprem, so an offline smoke result is never relabelled a
promotion verdict.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("run_eval", _REPO / "eval" / "run_eval.py")
run_eval = importlib.util.module_from_spec(_spec)
sys.modules["run_eval"] = run_eval  # so the module's dataclasses resolve their own module
_spec.loader.exec_module(run_eval)  # type: ignore[union-attr]


def test_smoke_mode_passes_offline(monkeypatch):
    monkeypatch.setenv("CDD_PROFILE", "local")
    assert run_eval.main(["--mode", "smoke"]) == 0


def test_default_mode_is_smoke(monkeypatch):
    monkeypatch.setenv("CDD_PROFILE", "local")
    assert run_eval.main([]) == 0


def test_gate_mode_refuses_local_profile(monkeypatch):
    monkeypatch.setenv("CDD_PROFILE", "local")
    with pytest.raises(SystemExit) as exc:
        run_eval.main(["--mode", "gate"])
    assert "platform" in str(exc.value) or "gcp" in str(exc.value)


def test_missing_dataset_is_exit_2(monkeypatch, tmp_path):
    monkeypatch.setenv("CDD_PROFILE", "local")
    assert run_eval.main(["--dataset", str(tmp_path / "nope.jsonl")]) == 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
