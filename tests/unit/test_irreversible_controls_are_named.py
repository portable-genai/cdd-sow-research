"""An irreversible control never arrives by default.

The audit bucket's lock is the one control in ``infra/terraform/`` that cannot be undone: a
locked Cloud Logging bucket refuses to be deleted, or to have its window shortened, for the whole
retention period, project owner or not. It used to carry ``default = true``, so an apply that said
nothing about the lock took a decision lasting the full retention window.

That is not hypothetical in this fleet. A sibling stack's bucket was locked for 2,557 days on a
first apply nobody reviewed, because its deployment file named neither the lock nor the retention
and the code default said ``true``. The API refuses to unlock it; the only exit taken was deleting
the project.

Observed failing first: restoring ``default = true`` to the variable turns
``test_the_lock_has_no_default_so_every_plan_names_it`` red, and rewriting the resource's
``locked`` to the literal turns ``test_the_audit_bucket_lock_is_a_variable_not_a_literal`` red.

The plan-level half of this proof lives in ``infra/terraform/tests/``, run by the repository's
Terraform target. This file is the half that needs no terraform binary, so the offline gate
holds it too.
"""

from __future__ import annotations

import re
from pathlib import Path

TF = Path(__file__).resolve().parents[2] / "infra" / "terraform"


def _variable_block(name: str) -> str:
    text = (TF / "variables.tf").read_text(encoding="utf-8")
    start = text.find(f'variable "{name}" {{')
    assert start != -1, f"variables.tf declares no {name!r}"
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise AssertionError(f"unterminated variable block {name!r}")


def test_the_audit_bucket_lock_is_a_variable_not_a_literal() -> None:
    worm = (TF / "logging_worm.tf").read_text(encoding="utf-8")
    assert re.search(r"^\s*locked\s*=\s*var\.worm_locked\s*$", worm, re.MULTILINE), (
        "the bucket's lock must read var.worm_locked, so a deployment can decline it"
    )
    assert not re.search(r"^\s*locked\s*=\s*true\s*$", worm, re.MULTILINE), (
        "a literal lock cannot be declined by any deployment"
    )


def test_the_lock_has_no_default_so_every_plan_names_it() -> None:
    block = _variable_block("worm_locked")
    assert not re.search(r"^\s*default\s*=", block, re.MULTILINE), (
        "worm_locked must have no default: an irreversible control must never be taken because a "
        "deployment said nothing, and a fork must never lose it the same way"
    )


def test_the_retention_floor_binds_only_when_the_bucket_is_locked() -> None:
    """An unlocked bucket may run a short, destroyable window; a locked one may not.

    The floor is the compliance control. Applying it to an unlocked stack would force a
    reference deployment to carry six months of logs it can delete anyway, which is why
    declining the lock is a supported posture rather than a weakened one.
    """
    block = _variable_block("retention_days")
    assert "var.worm_locked" in block, (
        "the retention floor must be conditional on the lock, not unconditional"
    )
