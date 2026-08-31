"""`next dev` must not write a second working agreement into ``ui/``.

Observed on Next 16.3.0 and 16.3.1: starting the dev server prints ``Generated AGENTS.md and
CLAUDE.md for AI agents`` and leaves both files in ``ui/``. Deleting them by hand does not hold,
because the next ``next dev`` writes them again. The writer is
``node_modules/next/dist/server/lib/generate-agent-files.js``, called from ``start-server.js``
behind an ``isDev`` check, which is why the CI production build never showed it.

Two things are wrong with them, and only one is cosmetic. The catalog's convention is that
``AGENTS.md`` is the working agreement and it is the ONLY one, with no tool-specific alias, and
this repo already carries its own at the root: ``ui/AGENTS.md`` is a second working agreement and
``ui/CLAUDE.md`` is exactly the alias the convention forbids. The generated prose also contains
an em-dash, which the catalog's house style forbids in shipped markdown.

``agentRules: false`` in ``ui/next.config.mjs`` is the fix, and the first assertion guards it.
The second is the one that matters more: a framework bump can rename or drop the option, and the
flag would then be a line nobody reads while the files come back. Asserting the files are ABSENT
fails on the artifact rather than on the spelling, so the guard survives the rename that would
defeat it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
UI = REPO_ROOT / "ui"

#: The two documents `next dev` writes into `ui/` when `agentRules` is not turned off.
GENERATED_AGENT_DOCUMENTS = ("AGENTS.md", "CLAUDE.md")

requires_ui = pytest.mark.skipif(not (UI / "package.json").exists(), reason="this repo has no ui/")


def _code_only(source: str) -> str:
    """``source`` with whole-line comments dropped, so prose quoting a flag is not code."""
    return "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith(("//", "*", "/*"))
    )


@requires_ui
def test_the_dev_server_does_not_write_a_second_working_agreement() -> None:
    code = _code_only((UI / "next.config.mjs").read_text(encoding="utf-8"))
    assert "agentRules: false" in code, (
        "next.config.mjs does not set `agentRules: false`, so `next dev` writes ui/AGENTS.md and "
        "ui/CLAUDE.md the moment somebody starts it. Confirm the option's current name against "
        "node_modules/next/dist/server/lib/generate-agent-files.js before changing this line."
    )
    present = [name for name in GENERATED_AGENT_DOCUMENTS if (UI / name).exists()]
    assert not present, (
        "ui/" + ", ui/".join(present) + " exists. This repo's working agreement is the AGENTS.md "
        "at its root and there is no tool-specific alias of it, so delete these; if `next dev` "
        "wrote them, the `agentRules` option in ui/next.config.mjs no longer disables the "
        "generation and the template needs fixing, not this repo."
    )
