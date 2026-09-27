"""The remote guardrail gateway's verdict allows only on a literal JSON ``true``.

The parser was ``allowed=bool(body.get("allowed", False))``. ``bool("false")`` is ``True``,
so a gateway that serialised its verdict as a string, or sent any other truthy non-boolean,
had a blocked screen read as an allow. Only the JSON literal ``true`` (Python ``True``)
allows now; every other value, including an absent key, blocks.
"""

from __future__ import annotations

from typing import Any

import pytest

from cdd_sow_research.adapters.platform.remote_guardrail import RemoteGuardrailAdapter
from cdd_sow_research.domain.models import Direction

_ABSENT = object()


def _verdict(allowed: Any) -> Any:
    body: dict[str, Any] = {"direction": "input", "reason": "gateway said so"}
    if allowed is not _ABSENT:
        body["allowed"] = allowed
    return RemoteGuardrailAdapter._parse_verdict(body, Direction.INPUT)


def test_a_literal_true_allows() -> None:
    assert _verdict(True).allowed is True


@pytest.mark.parametrize(
    "allowed",
    [False, _ABSENT, None, "false", "False", "true", "0", "no", 1, 1.0, [False], {"v": 0}],
    ids=[
        "false",
        "absent",
        "null",
        "str-false",
        "str-False",
        "str-true",
        "str-0",
        "str-no",
        "int-1",
        "float-1",
        "list",
        "object",
    ],
)
def test_anything_but_a_literal_true_blocks(allowed: Any) -> None:
    assert _verdict(allowed).allowed is False
