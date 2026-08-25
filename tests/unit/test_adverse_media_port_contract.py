"""Every adverse-media adapter must answer the port's question, not a different one.

The port draws a distinction the dossier renders differently: ``None`` means no screen ran,
an empty ``AdverseMediaScreening`` means a screen ran and found nothing. A bare list of
findings can carry only one of those facts.

The gcp adapter returned a list, and ``[]`` when disabled. Both halves were wrong, and the
first hid the second: the caller reads ``.findings`` off the result, so the whole assessment
died with ``AttributeError: 'list' object has no attribute 'findings'`` before anyone could
notice that a disabled backend was about to be reported as a clean screen.

Nothing offline caught it because the local and live adapters bind a different implementation,
and the annotation was self-consistently wrong -- the adapter declared the list it returned.
"""

from __future__ import annotations

import inspect

import pytest

from cdd_sow_research.domain.models import AdverseMediaScreening
from cdd_sow_research.ports.research import AdverseMediaPort

_ADAPTERS = (
    "cdd_sow_research.adapters.local.adverse_media:LocalCannedAdverseMediaAdapter",
    "cdd_sow_research.adapters.gcp.gemini_adverse_media:GeminiAdverseMediaAdapter",
    "cdd_sow_research.adapters.onprem.adverse_media:OnPremAdverseMediaAdapter",
)


def _load(dotted: str) -> type:
    import importlib

    module_path, _, class_name = dotted.partition(":")
    return getattr(importlib.import_module(module_path), class_name)


@pytest.mark.parametrize("dotted", _ADAPTERS, ids=lambda d: d.rsplit(":", 1)[-1])
def test_every_adapter_declares_the_port_s_return_type(dotted: str) -> None:
    """The annotation is the cheapest place to catch a shape mismatch, so pin it."""

    expected = inspect.signature(AdverseMediaPort.search).return_annotation
    actual = inspect.signature(_load(dotted).search).return_annotation
    assert str(actual) == str(expected), (
        f"{dotted} promises {actual!r}; the port promises {expected!r}. A bare list cannot "
        "distinguish 'screened, nothing found' from 'no screen ran'."
    )


def test_a_disabled_gcp_backend_reports_no_screen_rather_than_a_clean_one() -> None:
    """The permissive branch: empty must never stand in for absent."""

    adapter = _load(_ADAPTERS[1]).__new__(_load(_ADAPTERS[1]))
    adapter._enabled = False
    result = adapter.search("Meridian Harbour Holdings Pte Ltd")
    assert result is None, (
        "a disabled backend returned a screening object, which the dossier renders as an "
        "affirmative 'No adverse media found' for a search that never happened"
    )
    assert not isinstance(result, AdverseMediaScreening)
