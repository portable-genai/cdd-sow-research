"""No em-dash in the text the demo pages and the domain put in front of a reader.

Business-facing text carries no em-dash. Outside the console, three places write it: the
static demo renderer, the presenter demo server's page chrome, and the domain, whose
headings, reasons and refusals reach the console and anything exported. Comments, docstrings
and presenter narration are not shown to a buyer and may carry one. The console's own text is
held by ``ui/tests/visible-text-has-no-em-dash.test.mjs``.

The check parses each module and reads only its string literals, so a docstring is never
mistaken for text on a page, and a lone placeholder for an empty cell is caught the same way a
heading is. Prompt templates are left out: a model reads them, not a person.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
DOMAIN = REPO_ROOT / "src" / "cdd_sow_research" / "domain"

DASH = chr(0x2014)
#: The character, and the HTML entities a renderer could write it as.
MARKS = (DASH, "&mdash;", "&#8212;", "&#x2014;")

#: One line of each kind the scan has to tell apart. Lines 4, 5, 6 and 9 are text a reader
#: would see; the two docstrings and the comment are not.
PROBE = f"""
'''A module docstring {DASH} nobody reads this on a page.'''
# A comment {DASH} nor this.
HEADING = "Subject {DASH} a borrower"
BANNER = f"<h1>Case {DASH} {{title}}</h1>"
CELL = "&mdash;"
def refuse():
    '''A function docstring {DASH} still not shown.'''
    raise ValueError("not merely mislabelled {DASH} invisible")
"""


def surfaces() -> list[Path]:
    """Every module whose string literals can end up in front of a reader."""
    rendered = [*SCRIPTS.glob("render_*_ui.py"), *SCRIPTS.glob("*_demo_server.py")]
    domain = [path for path in DOMAIN.rglob("*.py") if path.name != "prompts.py"]
    return sorted(rendered + domain)


def em_dash_literals(source: str) -> list[int]:
    """The line of every string literal in ``source`` carrying an em-dash, docstrings aside."""
    tree = ast.parse(source)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    lines = source.lower().splitlines()
    found: list[int] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and any(mark in node.value.lower() for mark in MARKS)
        ):
            # Pieces of a string split across lines merge into one literal that reports the
            # line it starts on, so name the line that actually carries the mark.
            span = range(node.lineno, (node.end_lineno or node.lineno) + 1)
            marked = (n for n in span if any(mark in lines[n - 1] for mark in MARKS))
            found.append(next(marked, node.lineno))
    return sorted(found)


def test_the_scan_reads_literals_and_leaves_docstrings_and_comments_alone() -> None:
    assert em_dash_literals(PROBE) == [4, 5, 6, 9]


def test_no_business_facing_text_carries_an_em_dash() -> None:
    modules = surfaces()
    assert len(modules) > 2, "the scan found almost nothing to read, so it checked nothing"
    found = [
        f"{path.relative_to(REPO_ROOT)}:{line}"
        for path in modules
        for line in em_dash_literals(path.read_text(encoding="utf-8"))
    ]
    assert not found, (
        "business-facing text carries an em-dash at " + ", ".join(found) + ". Rewrite it with "
        "a colon, a comma, parentheses or two sentences; an empty value reads n/a."
    )
