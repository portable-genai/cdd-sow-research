"""Local ownership-graph adapter (OwnershipGraphPort) — a fictional layered structure.

The ``local`` profile's stand-in for the grounded registry hop: a deterministic, in-process fixture
that serves one cited hop at a time for an **entirely invented** multi-jurisdiction structure, so
the whole UBO journey (walk, effective ownership, the control ladder, the indicators, the
human-review-console route) runs end to end with no Google Cloud and no API key. SDK-free and
unconditional (there is no emulator for a company registry).

The fixture is deliberately shaped to exercise every branch the engine has:

* a **layered chain** (a person above two stacked holding companies in two jurisdictions),
  so effective ownership is a product rather than a single reading;
* a **cross-holding cycle** (two invented holdcos owning each other), so the visited set
  and the simple-path rule are exercised rather than merely asserted;
* a **nominee director** whose name recurs across otherwise unrelated invented entities
  and who sits at a shared registered-agent address; and
* a **shell pass-through** layer: a newly incorporated, dormant, single-owner company in
  a higher-risk jurisdiction that holds exactly one asset.

Any subject the fixture does not already know is served the subject hop, so an offline
demo of an arbitrary entity still produces a complete, cited answer. Every name is
marked FICTIONAL, the registered address names an invented town and every URL is
``example.test``: this fixture never asserts anything about a real party. The
jurisdiction codes ARE real ISO codes, because the secrecy-jurisdiction indicator
scores through the shipped FATF-derived country lists and a made-up code would score
nothing.
"""

from __future__ import annotations

from ...config import Settings
from ...domain.models import (
    Citation,
    OwnershipEdge,
    OwnershipEdgeKind,
    OwnershipGraphNode,
    OwnershipNodeKind,
    RegistryHop,
    SourceType,
)
from ...domain.name_match import normalize
from ...domain.ubo_graph import ownership_node_id

#: Where each invented party is registered. Real ISO codes on purpose (see the module
#: docstring): the shell sits in a FATF increased-monitoring jurisdiction so the
#: secrecy-jurisdiction indicator is actually exercised offline.
_JURISDICTIONS = {
    "operating": "SG",
    "midco": "KY",
    "topco": "JE",
    "cycle_a": "KY",
    "cycle_b": "VG",
    "shell": "VE",
}

_AGENT_ADDRESS = "Unit 1, Registered Agent House, Invented Town (FICTIONAL)"


def _citation(entity: str, page: int, part: str = "profile") -> Citation:
    """One cited registry page. ``part`` keeps the profile page and the holdings page
    distinct source ids, so a path's citations name the filing the percentage came from
    rather than collapsing into a single undifferentiated extract."""
    return Citation(
        source_id=f"registry-hop:{'-'.join(normalize(entity).split())}:{part}",
        source_type=SourceType.REGISTRY,
        title=f"Corporate registry extract for {entity} (FICTIONAL)",
        url="https://example.test/doc-registry",
        page=page,
        snippet=f"Ownership and officers recorded against {entity}.",
        score=0.9,
    )


def _node(
    name: str,
    jurisdiction: str,
    kind: OwnershipNodeKind,
    *,
    address: str = "",
    incorporated: str = "",
    status: str = "",
    is_pep: bool = False,
    page: int = 2,
) -> OwnershipGraphNode:
    return OwnershipGraphNode(
        id=ownership_node_id(name, jurisdiction),
        name=name,
        kind=kind,
        jurisdiction=jurisdiction,
        registered_address=address,
        incorporation_date=incorporated,
        status=status,
        is_pep=is_pep,
        citations=(_citation(name, page),),
    )


def _edge(
    owner: OwnershipGraphNode,
    owned: OwnershipGraphNode,
    kind: OwnershipEdgeKind,
    pct: float = 0.0,
) -> OwnershipEdge:
    return OwnershipEdge(
        source_id=owner.id,
        target_id=owned.id,
        kind=kind,
        pct=pct,
        as_of="2026-01-31",
        citations=(_citation(owned.name, 3, "holders"),),
    )


# --------------------------------------------------------------------------- #
# The invented parties
# --------------------------------------------------------------------------- #
MIDCO = _node(
    "Palewater Midco Ltd (FICTIONAL)",
    _JURISDICTIONS["midco"],
    OwnershipNodeKind.ENTITY,
    address=_AGENT_ADDRESS,
    incorporated="2014-03-02",
    status="active",
)
TOPCO = _node(
    "Palewater Topco Ltd (FICTIONAL)",
    _JURISDICTIONS["topco"],
    OwnershipNodeKind.ENTITY,
    address=_AGENT_ADDRESS,
    incorporated="2011-07-19",
    status="active",
)
CYCLE_A = _node(
    "Ouroboros Holdings A Ltd (FICTIONAL)",
    _JURISDICTIONS["cycle_a"],
    OwnershipNodeKind.ENTITY,
    address=_AGENT_ADDRESS,
    incorporated="2016-05-05",
    status="active",
)
CYCLE_B = _node(
    "Ouroboros Holdings B Ltd (FICTIONAL)",
    _JURISDICTIONS["cycle_b"],
    OwnershipNodeKind.ENTITY,
    incorporated="2016-05-06",
    status="active",
)
SHELL = _node(
    "Thin Air Pass-Through Ltd (FICTIONAL)",
    _JURISDICTIONS["shell"],
    OwnershipNodeKind.ENTITY,
    address=_AGENT_ADDRESS,
    incorporated="2026-05-01",
    status="dormant",
)
PRINCIPAL = _node(
    "Ines Quiller (FICTIONAL)",
    "JE",
    OwnershipNodeKind.NATURAL_PERSON,
)
MINORITY = _node(
    "Odile Vantry (FICTIONAL)",
    "SG",
    OwnershipNodeKind.NATURAL_PERSON,
)
NOMINEE_DIRECTOR = _node(
    "Corvin Sable (FICTIONAL)",
    "KY",
    OwnershipNodeKind.NATURAL_PERSON,
    address=_AGENT_ADDRESS,
)
NOMINEE_HOLDER = _node(
    "Sable Nominee Services Ltd (FICTIONAL)",
    "KY",
    OwnershipNodeKind.NOMINEE,
    address=_AGENT_ADDRESS,
    incorporated="2009-02-11",
    status="active",
)


class LocalOwnershipGraphAdapter:
    """Serve one cited hop of a deterministic, entirely fictional structure."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def hop(self, entity_name: str, jurisdiction: str) -> RegistryHop:
        """Return the parties recorded directly against ``entity_name``."""
        node_id = ownership_node_id(entity_name, jurisdiction)
        for known, builder in (
            (MIDCO.id, self._midco),
            (TOPCO.id, self._topco),
            (CYCLE_A.id, self._cycle_a),
            (CYCLE_B.id, self._cycle_b),
            (SHELL.id, self._shell),
            (NOMINEE_HOLDER.id, self._nominee_holder),
        ):
            if node_id == known:
                return builder()
        if node_id in (PRINCIPAL.id, MINORITY.id, NOMINEE_DIRECTOR.id):
            # A natural person is a leaf: nobody owns a person.
            return RegistryHop(entity=self._person_for(node_id))
        return self._subject(entity_name, jurisdiction)

    # ----------------------------------------------------------------- hops
    def _subject(self, entity_name: str, jurisdiction: str) -> RegistryHop:
        """The subject's own hop: two holding layers, a nominee holder and a director."""
        # The node id must be derived from the jurisdiction the CALLER gave, because the
        # engine minted the root id the same way; deriving it from the fixture's default
        # instead would silently orphan every edge below.
        subject = OwnershipGraphNode(
            id=ownership_node_id(entity_name, jurisdiction),
            name=entity_name,
            kind=OwnershipNodeKind.ENTITY,
            jurisdiction=jurisdiction or _JURISDICTIONS["operating"],
            incorporation_date="2012-09-14",
            status="active",
            citations=(_citation(entity_name, 2),),
        )
        owners = (MIDCO, CYCLE_A, NOMINEE_HOLDER, MINORITY, NOMINEE_DIRECTOR)
        edges = (
            _edge(MIDCO, subject, OwnershipEdgeKind.SHAREHOLDING, 60.0),
            _edge(CYCLE_A, subject, OwnershipEdgeKind.SHAREHOLDING, 15.0),
            _edge(NOMINEE_HOLDER, subject, OwnershipEdgeKind.SHAREHOLDING, 15.0),
            _edge(NOMINEE_HOLDER, subject, OwnershipEdgeKind.NOMINEE_ARRANGEMENT),
            _edge(MINORITY, subject, OwnershipEdgeKind.SHAREHOLDING, 10.0),
            _edge(NOMINEE_DIRECTOR, subject, OwnershipEdgeKind.DIRECTORSHIP),
        )
        return RegistryHop(
            entity=subject, owners=owners, edges=edges, citations=(_citation(entity_name, 2),)
        )

    def _midco(self) -> RegistryHop:
        """The layered chain: Topco above Midco, plus the shell pass-through beside it."""
        edges = (
            _edge(TOPCO, MIDCO, OwnershipEdgeKind.SHAREHOLDING, 80.0),
            _edge(SHELL, MIDCO, OwnershipEdgeKind.SHAREHOLDING, 20.0),
            _edge(NOMINEE_DIRECTOR, MIDCO, OwnershipEdgeKind.DIRECTORSHIP),
        )
        return RegistryHop(
            entity=MIDCO,
            owners=(TOPCO, SHELL, NOMINEE_DIRECTOR),
            edges=edges,
            citations=(_citation(MIDCO.name, 2),),
        )

    def _topco(self) -> RegistryHop:
        """The top of the chain: one natural person and a co-investing holdco."""
        edges = (
            _edge(PRINCIPAL, TOPCO, OwnershipEdgeKind.SHAREHOLDING, 75.0),
            _edge(CYCLE_B, TOPCO, OwnershipEdgeKind.SHAREHOLDING, 25.0),
            _edge(NOMINEE_DIRECTOR, TOPCO, OwnershipEdgeKind.DIRECTORSHIP),
        )
        return RegistryHop(
            entity=TOPCO,
            owners=(PRINCIPAL, CYCLE_B, NOMINEE_DIRECTOR),
            edges=edges,
            citations=(_citation(TOPCO.name, 2),),
        )

    def _cycle_a(self) -> RegistryHop:
        """Half of the cross-holding: A is owned by B."""
        return RegistryHop(
            entity=CYCLE_A,
            owners=(CYCLE_B,),
            edges=(_edge(CYCLE_B, CYCLE_A, OwnershipEdgeKind.SHAREHOLDING, 100.0),),
            citations=(_citation(CYCLE_A.name, 2),),
        )

    def _cycle_b(self) -> RegistryHop:
        """The other half: B is owned by A. The walk must terminate here, not loop."""
        return RegistryHop(
            entity=CYCLE_B,
            owners=(CYCLE_A,),
            edges=(_edge(CYCLE_A, CYCLE_B, OwnershipEdgeKind.SHAREHOLDING, 100.0),),
            citations=(_citation(CYCLE_B.name, 2),),
        )

    def _shell(self) -> RegistryHop:
        """The pass-through: one owner, one asset, newly formed, dormant."""
        return RegistryHop(
            entity=SHELL,
            owners=(NOMINEE_HOLDER,),
            edges=(_edge(NOMINEE_HOLDER, SHELL, OwnershipEdgeKind.SHAREHOLDING, 100.0),),
            citations=(_citation(SHELL.name, 2),),
        )

    def _nominee_holder(self) -> RegistryHop:
        """The nominee company: the registry records no owner above it (opaque)."""
        return RegistryHop(
            entity=NOMINEE_HOLDER,
            resolved=False,
            citations=(_citation(NOMINEE_HOLDER.name, 2),),
        )

    @staticmethod
    def _person_for(node_id: str) -> OwnershipGraphNode:
        for person in (PRINCIPAL, MINORITY, NOMINEE_DIRECTOR):
            if person.id == node_id:
                return person
        return PRINCIPAL
