"""Local evaluation-gate adapter (EvaluationGatePort) — delegates to the offline gate.

The ``local`` profile's stand-in for the **Gen AI evaluation service** (A4): it delegates
to the in-repo offline evaluator (``eval/run_eval.py``), the same deterministic,
credential-free heuristic gate that guards CI. SDK-free and unconditional. This proves the
A4 promotion gate runs entirely off-cloud.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from ...config import Settings
from ...domain.models import EvalReport

# eval/ is a repo-root directory (not a package): adapters/local/evaluation.py ->
# local -> adapters -> cdd_sow_research -> src -> <repo>.
_REPO_ROOT = Path(__file__).resolve().parents[4]
_EVAL_SCRIPT = _REPO_ROOT / "eval" / "run_eval.py"


class LocalOfflineEvalAdapter:
    """Run the in-repo offline eval gate and return its :class:`EvalReport`."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def evaluate(self, dataset_path: str) -> EvalReport:
        run_eval = self._load_run_eval()
        if run_eval is not None:
            dataset = Path(dataset_path) if dataset_path else run_eval.DEFAULT_DATASET
            thresholds = run_eval.load_thresholds_from_rubrics()
            return run_eval.run_offline(dataset, thresholds)
        # Defensive fallback: the offline gate script is unavailable in this build.
        # Nothing was evaluated, so the report claims nothing. The old fallback returned a
        # hand-written passing metric over zero examples, and `gate()` is
        # `self.evaluate("").passed`, so the promotion gate approved exactly when the
        # evaluator was missing. An empty report fails closed and invents no evidence.
        return EvalReport(dataset=dataset_path, results=(), n_examples=0)

    def gate(self, target: str) -> bool:
        """Promotion gate: True iff the offline eval over the default dataset passes."""
        return self.evaluate("").passed

    @staticmethod
    def _load_run_eval():  # type: ignore[no-untyped-def]
        if not _EVAL_SCRIPT.exists():
            return None
        spec = importlib.util.spec_from_file_location("cdd_local_run_eval", _EVAL_SCRIPT)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules.setdefault("cdd_local_run_eval", module)
        spec.loader.exec_module(module)
        return module
