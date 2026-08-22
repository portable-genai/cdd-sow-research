"""Backward-compatible re-export: the store now ships inside the package.

``InMemoryCaseStore`` moved to :mod:`cdd_sow_research.adapters.local.case_store` so demo
scripts import it with only ``PYTHONPATH=src`` (no reach into ``tests/``). This shim
keeps older imports working.
"""

from __future__ import annotations

from cdd_sow_research.adapters.local.case_store import InMemoryCaseStore

__all__ = ["InMemoryCaseStore"]
