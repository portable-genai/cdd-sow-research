"""A model client must call the MODEL location, never the compute region.

Constructed with `settings.region` until 2026-08-27, every Gemini client in this repository
was capped at whatever the deploy region serves. us-central1 serves the 2.5 family and no
Gemini 3 at all, so `config/settings.yaml` could pin -- and did pin -- model ids that could
never resolve on the deployment. `gemini-3.1-pro` sat in the hard-reasoning slot resolving in
NO location, and nothing failed until someone switched that path on.

Probed against the named deployment on 2026-08-27: `gemini-3.7-flash` answers in `us`, and
404s in us-central1 and us-east5.

`us` rather than `global` is the load-bearing half. A multi-region endpoint pools capacity
across regions within one geography and Google documents ML processing as staying inside it;
the global endpoint carries no residency guarantee. Using `global` would reach the same models
and quietly break the `in:us-locations` claim the Org Policy enforces and the dossiers make.

The guard asserts the SOURCE, not a value: a client rebuilt with the compute region would pass
any assertion about which string `models.location` happens to hold.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ADAPTERS = Path("src/cdd_sow_research/adapters/gcp")


def _client_location_args(path: Path) -> list[str]:
    """Every `location=` keyword passed to a genai.Client(...) call in ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name != "Client":
            continue
        for kw in node.keywords:
            if kw.arg == "location":
                found.append(ast.unparse(kw.value))
    return found


def test_no_model_client_is_built_with_the_compute_region() -> None:
    offenders: dict[str, list[str]] = {}
    for path in sorted(_ADAPTERS.glob("*.py")):
        bad = [expr for expr in _client_location_args(path) if expr.endswith("settings.region")]
        if bad:
            offenders[path.name] = bad

    assert not offenders, (
        "these model clients call the compute region, so they can only reach what the deploy "
        f"region serves: {offenders}"
    )


def test_every_model_client_uses_the_reviewed_model_location() -> None:
    seen = 0
    for path in sorted(_ADAPTERS.glob("*.py")):
        for expr in _client_location_args(path):
            seen += 1
            assert expr.endswith("models.location"), f"{path.name}: location={expr}"
    assert seen >= 4, f"expected the known model clients to be found, saw {seen}"


def test_the_default_location_is_a_residency_bearing_multi_region() -> None:
    from cdd_sow_research.config import ModelSettings

    assert ModelSettings().location == "us"
    # `global` reaches the same models and carries NO residency guarantee. If this default ever
    # becomes "global", the in:us-locations claim in the dossiers stops being true.
    assert ModelSettings().location != "global"
