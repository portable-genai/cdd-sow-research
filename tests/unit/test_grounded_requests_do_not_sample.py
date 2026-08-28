"""The grounded request must not sample by default.

The organization front page claims, for every repository here, that the consequential math is
deterministic and replayable and that the model "never produces the number". That sentence was
measured in exactly one tree, `cdd-sow-research`, because it is the only one with a paired
demonstration to measure it against, and there it was FALSE. On 2026-08-26 two runs of one
identical case against the deployment, minutes apart, returned `score` 0.5 then 0.0,
`confidence` 0.4 then 1.0, and four scorecard factors then none. The cause was not in any one
service: the shared request builder defaulted to `temperature=0.2`, so every grounded call
sampled.

This file is that finding applied here rather than left as one repository's history. The default
belongs on the type and on the builder, because a call site that omits `temperature` is the
common case and it inherits whatever the default is.

**Temperature 0 is not a promise of determinism, and nothing here asserts one.** A hosted model
can still vary across batching and model revisions. It is the strongest thing a caller controls,
and it is what makes a comparison between two profiles a measurement rather than a sample.
"""

from __future__ import annotations

import inspect

from cdd_sow_research.domain import _grounded as _b0
from cdd_sow_research.domain.kernel import LlmRequest


def test_the_request_type_does_not_sample_by_default() -> None:
    """A call site that omits temperature is the common case, so the type carries the pin."""
    assert LlmRequest.__dataclass_fields__["temperature"].default == 0.0


def test_the_builder_in__grounded_0_does_not_sample_by_default() -> None:
    signature = inspect.signature(_b0.build_llm_request)

    assert signature.parameters["temperature"].default == 0.0
