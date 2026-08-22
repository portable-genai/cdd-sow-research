"""OwnershipGraphPort: ONE cited registry hop, so the engine can walk the structure.

``CorporateRegistryPort`` answers "who are the owners of record for this entity" in one
flat shot, which is the shape the dossier wants. A layered cross-border structure is not
answerable that way: the 30% beneficial owner of an operating company may sit two holding
companies and two jurisdictions away, and no single extract shows them.

This port therefore has ONE method and returns ONE hop: the parties a registry records
directly against a single entity, each carrying its own :class:`Citation`. Traversal is
deliberately NOT here:

* an adapter that walked the chain itself would bury the depth limit, the visited set and
  the truncation rule inside a vendor integration, where no auditor can see them and no
  compliance function can retune them; and
* an adapter that walked the chain would make the answer non-replayable, because the walk
  would depend on whatever the provider felt like returning that day.

So the pure engine (``domain/ubo_graph.py``) owns the breadth-first walk and asks this
port for one hop at a time, and every threshold it applies comes from bank-owned policy.

Bound per profile like every other port: ``gcp``/``platform``/``live`` use the grounded
managed lookup (lazy SDK imports), ``local`` serves an obviously fictional multi-
jurisdiction fixture so the whole journey runs offline, and ``onprem`` is the fail-fast
sovereign placeholder that refuses to invent a layer.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..domain.models import RegistryHop


@runtime_checkable
class OwnershipGraphPort(Protocol):
    def hop(self, entity_name: str, jurisdiction: str) -> RegistryHop:
        """Return the ONE registry hop recorded directly against ``entity_name``.

        Implementations return a hop with ``resolved=False`` (rather than raising) when
        the registry has no answer for the entity: an opaque layer is a finding the
        engine flags, not an error that loses the rest of the structure.
        """
        ...
