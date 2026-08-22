"""UBO-graph engine: deterministic cross-jurisdiction ownership resolution.

``OwnershipService`` answers "who are the owners of record" in one flat hop, which is what
the dossier consumes. This module answers the harder question a layered cross-border
structure poses: a natural person holding 60% of a holding company that holds 50% of the
operating entity is a 30% beneficial owner no single registry extract shows.

It is the pure, replayable half of the module:

* :func:`ownership_node_id` turns a party's name plus jurisdiction into a stable traversal
  key, so the same real party is the same node on every run and a cycle is detectable;
* :meth:`UboGraphEngine.traverse` walks the structure breadth-first through a caller-
  supplied hop fetcher, with a VISITED SET so cross-holdings and circular ownership
  terminate, and with depth/node/path limits that set the truncation flag rather than
  running away;
* :meth:`UboGraphEngine.findings` computes effective ownership as the sum over SIMPLE
  paths of the product of the shareholding percentages along each path, deterministically
  rounded, one finding per candidate party, each carrying the paths it came from and
  those paths' citations;
* :meth:`UboGraphEngine.control` applies an explicit ladder (effective majority, voting
  majority, board majority, contractual control, then the senior-managing-official
  FALLBACK when nobody reaches the threshold); and
* :meth:`UboGraphEngine.indicators` raises nominee and shell INDICATORS and turns them
  into a deterministic opacity score.

Every number here is arithmetic over bank-owned policy
(:class:`~cdd_sow_research.domain.policy.UboGraphPolicy`). **The LLM never produces a node,
an edge, a percentage or a verdict in this module and is not imported by it**: it narrates
the finished resolution elsewhere. Given the same hops and the same ``as_of``, the whole
resolution is byte-identical, so an auditor can recompute it.

A resolution is consequential decision support: it always sets ``requires_human_review``
and never auto-blocks. The orchestrator (``ubo_graph_service``) routes it to Hrz7 for
maker-checker disposition (rule R8). Indicators are INDICATORS, never conclusions.

Pure standard library; no ports, no I/O, no clock of its own (the caller supplies
``as_of`` and the hop fetcher).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time

from .country_risk import country_risk
from .models import (
    BeneficialOwner,
    Citation,
    ControlBasis,
    OwnershipEdge,
    OwnershipEdgeKind,
    OwnershipFlag,
    OwnershipFlagKind,
    OwnershipGraph,
    OwnershipGraphNode,
    OwnershipNode,
    OwnershipNodeKind,
    OwnershipPath,
    OwnershipPathStep,
    OwnershipSummary,
    RegistryHop,
    Severity,
    Subject,
    UboFinding,
    UboResolution,
)
from .name_match import name_score, normalize
from .policy import CountryRiskPolicy, UboGraphPolicy

#: The one thing the engine needs from the outside world: given an entity's name and
#: jurisdiction, ONE cited registry hop. A plain callable rather than a port, so the
#: engine stays importable with no ports package and testable from a dict.
HopFetcher = Callable[[str, str], RegistryHop]

#: Only equity multiplies into an effective percentage. Voting, directorship, nominee and
#: contractual edges establish CONTROL and are read by the ladder, but multiplying a board
#: seat into an ownership percentage would be arithmetic nobody could defend.
_OWNERSHIP_EDGE_KINDS = frozenset({OwnershipEdgeKind.SHAREHOLDING})

#: Default opacity-to-review-severity bands for a bare ``UboGraphEngine()``; the real
#: values are bank-owned policy (``UboGraphPolicy.opacity_severity_bands``) threaded through
#: :meth:`UboGraphEngine.from_policy`, so no review threshold is buried in the engine (B4).
#: Descending ``(floor, severity)``: the first band a score clears is the one it earns.
_DEFAULT_OPACITY_BANDS: tuple[tuple[float, str], ...] = (
    (0.75, "critical"),
    (0.50, "high"),
    (0.25, "medium"),
)


def ownership_node_id(name: str, jurisdiction: str = "") -> str:
    """A stable traversal key for one party: ``<normalised-name>@<jurisdiction>``.

    Derived from the same normalisation the screening name matcher uses (case, accents
    and punctuation folded away), so a registry rendering the same company as "Acme
    Holdings Pte. Ltd." and "ACME HOLDINGS PTE LTD" yields ONE node rather than two, and a
    circular holding is therefore detectable instead of looking like an infinite chain.
    """
    slug = "-".join(normalize(name).split()) or "unnamed"
    return f"{slug}@{(jurisdiction or 'unknown').strip().lower()}"


def _at(as_of: date) -> datetime:
    """The single timestamp a run stamps on everything it produces (replayability)."""
    return datetime.combine(as_of, time.min, tzinfo=UTC)


def _round(value: float, decimals: int) -> float:
    return round(value + 0.0, decimals)


def _dedupe(citations: Sequence[Citation]) -> tuple[Citation, ...]:
    seen: set[str] = set()
    out: list[Citation] = []
    for citation in citations:
        if citation.source_id in seen:
            continue
        seen.add(citation.source_id)
        out.append(citation)
    return tuple(out)


@dataclass(frozen=True, slots=True)
class UboGraphEngine:
    """Pure, deterministic UBO-graph traversal, effective ownership and control."""

    #: A natural person at/above this effective percentage is a beneficial owner.
    ownership_threshold_pct: float = 25.0
    #: Effective percentage at/above which a party CONTROLS (ladder rung 1).
    control_threshold_pct: float = 50.0
    #: Direct voting percentage at/above which a party controls (ladder rung 2).
    voting_threshold_pct: float = 50.0
    #: Share of the subject's recorded board seats that constitutes control (rung 3).
    board_majority_ratio: float = 0.5
    #: Hops from the subject the walk may take before it truncates.
    max_depth: int = 6
    #: Nodes the walk may admit before it truncates (a runaway-structure guard).
    max_nodes: int = 200
    #: Simple paths enumerated per candidate before the enumeration truncates.
    max_paths: int = 64
    #: Decimal places every percentage is rounded to (byte-identical replays).
    pct_decimals: int = 4
    #: Distinct entities one name must appear against before it reads as a nominee.
    nominee_name_recurrence: int = 3
    #: Name-similarity at/above which two recorded names are treated as one party.
    nominee_name_threshold: float = 0.90
    #: Entities sharing one registered address before the address reads as an agent's.
    shared_address_entities: int = 2
    #: Tokens in a party's name that declare a nominee/fiduciary role.
    nominee_tokens: tuple[str, ...] = ()
    #: Single-owner holding at/above this percentage makes a layer a pass-through.
    pass_through_pct: float = 90.0
    #: Days since incorporation below which an entity counts as newly formed.
    young_entity_days: int = 365
    #: Registry filing statuses that read as dormant / struck off.
    dormant_statuses: tuple[str, ...] = ()
    #: Distinct shell signals one layer must show before the indicator is raised.
    min_shell_signals: int = 2
    #: Opacity weight per flag kind (the deterministic score, clamped to [0, 1]).
    flag_weight: Mapping[str, float] = field(default_factory=dict)
    #: Descending ``(floor, severity)`` bands mapping opacity to the Hrz7 review severity.
    opacity_severity_bands: tuple[tuple[float, str], ...] = _DEFAULT_OPACITY_BANDS
    #: The FATF-derived country lists the secrecy-jurisdiction indicator scores through.
    country_risk_policy: CountryRiskPolicy = field(default_factory=CountryRiskPolicy)

    @classmethod
    def from_policy(cls, policy: UboGraphPolicy, country: CountryRiskPolicy) -> UboGraphEngine:
        """Build the engine from bank-owned policy: every number is adopter-owned (B4)."""
        return cls(
            ownership_threshold_pct=policy.ownership_threshold_pct,
            control_threshold_pct=policy.control_threshold_pct,
            voting_threshold_pct=policy.voting_threshold_pct,
            board_majority_ratio=policy.board_majority_ratio,
            max_depth=policy.max_depth,
            max_nodes=policy.max_nodes,
            max_paths=policy.max_paths,
            pct_decimals=policy.pct_decimals,
            nominee_name_recurrence=policy.nominee_name_recurrence,
            nominee_name_threshold=policy.nominee_name_threshold,
            shared_address_entities=policy.shared_address_entities,
            nominee_tokens=tuple(policy.nominee_tokens),
            pass_through_pct=policy.pass_through_pct,
            young_entity_days=policy.young_entity_days,
            dormant_statuses=tuple(policy.dormant_statuses),
            min_shell_signals=policy.min_shell_signals,
            flag_weight=dict(policy.flag_weight),
            opacity_severity_bands=tuple(policy.opacity_severity_bands),
            country_risk_policy=country,
        )

    # ------------------------------------------------------------------ #
    # 1. Traversal
    # ------------------------------------------------------------------ #
    def traverse(self, *, subject: Subject, as_of: date, fetch: HopFetcher) -> OwnershipGraph:
        """Walk the structure breadth-first from ``subject`` and return the graph.

        The visited set is what makes this terminate: a cross-holding (A owns B, B owns A)
        or a longer circular chain revisits a node id already walked, so the edge is
        recorded but the node is not expanded again. Depth and node limits set
        ``truncated`` rather than silently returning a partial picture as if it were whole.
        """
        root_id = ownership_node_id(subject.name, subject.jurisdiction)
        nodes: dict[str, OwnershipGraphNode] = {
            root_id: OwnershipGraphNode(
                id=root_id,
                name=subject.name,
                kind=OwnershipNodeKind.ENTITY,
                jurisdiction=subject.jurisdiction,
                depth=0,
            )
        }
        edges: list[OwnershipEdge] = []
        seen_edges: set[tuple[str, str, str]] = set()
        unresolved: list[str] = []
        visited: set[str] = set()
        frontier: list[tuple[str, int]] = [(root_id, 0)]
        deepest = 0
        truncated = False

        while frontier:
            node_id, depth = frontier.pop(0)
            if node_id in visited:
                continue
            visited.add(node_id)
            node = nodes[node_id]
            if node.kind is OwnershipNodeKind.NATURAL_PERSON:
                # A natural person is a leaf by definition: nobody owns a person.
                continue
            if depth >= self.max_depth:
                truncated = True
                continue

            hop = fetch(node.name, node.jurisdiction)
            nodes[node_id] = _merge_node(node, hop.entity, depth)
            if not hop.resolved:
                unresolved.append(node_id)
                continue

            next_frontier: list[tuple[str, int]] = []
            for owner in hop.owners:
                if owner.id not in nodes:
                    if len(nodes) >= self.max_nodes:
                        truncated = True
                        break
                    nodes[owner.id] = replace(owner, depth=depth + 1)
                    deepest = max(deepest, depth + 1)
                if owner.id not in visited:
                    next_frontier.append((owner.id, depth + 1))
            for edge in hop.edges:
                key = (edge.source_id, edge.target_id, edge.kind.value)
                if key in seen_edges or edge.source_id not in nodes or edge.target_id not in nodes:
                    continue
                seen_edges.add(key)
                edges.append(edge)
            frontier.extend(next_frontier)

        ordered_nodes = tuple(sorted(nodes.values(), key=lambda n: (n.depth, n.id)))
        return OwnershipGraph(
            root_id=root_id,
            root_name=subject.name,
            nodes=ordered_nodes,
            edges=tuple(sorted(edges, key=lambda e: (e.target_id, e.source_id, e.kind.value))),
            depth=deepest,
            truncated=truncated,
            unresolved_ids=tuple(sorted(unresolved)),
            jurisdictions=tuple(sorted({n.jurisdiction for n in ordered_nodes if n.jurisdiction})),
            as_of=as_of.isoformat(),
        )

    # ------------------------------------------------------------------ #
    # 2. Effective ownership
    # ------------------------------------------------------------------ #
    def findings(self, graph: OwnershipGraph) -> tuple[UboFinding, ...]:
        """One finding per candidate party, with its effective percentage and paths.

        Effective ownership is the sum over SIMPLE paths (no node repeated, which is what
        keeps a circular holding finite) of the product of the shareholding percentages
        along each path. Candidates are every party that reaches the subject through
        equity, plus every party holding a direct control edge on the subject, so the
        ladder below always has someone to consider.
        """
        by_id = {node.id: node for node in graph.nodes}
        candidates = {edge.source_id for edge in graph.edges if edge.source_id in by_id} - {
            graph.root_id
        }
        out: list[UboFinding] = []
        for node_id in sorted(candidates):
            node = by_id[node_id]
            paths = self._paths(graph, node_id)
            total = _round(sum(p.product_pct for p in paths), self.pct_decimals)
            out.append(
                UboFinding(
                    node_id=node_id,
                    name=node.name,
                    kind=node.kind,
                    jurisdiction=node.jurisdiction,
                    is_pep=node.is_pep,
                    effective_pct=total,
                    paths=paths,
                    meets_threshold=(
                        node.kind is OwnershipNodeKind.NATURAL_PERSON
                        and total >= self.ownership_threshold_pct
                    ),
                    citations=_dedupe([*node.citations, *(c for p in paths for c in p.citations)]),
                )
            )
        return tuple(sorted(out, key=lambda f: (-f.effective_pct, f.node_id)))

    def _paths(self, graph: OwnershipGraph, owner_id: str) -> tuple[OwnershipPath, ...]:
        """Every simple equity path from ``owner_id`` down to the subject, with working."""
        by_id = {node.id: node for node in graph.nodes}
        owners_of: dict[str, list[OwnershipEdge]] = {}
        for edge in graph.edges:
            if edge.kind in _OWNERSHIP_EDGE_KINDS:
                owners_of.setdefault(edge.target_id, []).append(edge)

        found: list[OwnershipPath] = []

        def walk(target_id: str, chain: list[OwnershipEdge], seen: frozenset[str]) -> None:
            if len(found) >= self.max_paths:
                return
            for edge in owners_of.get(target_id, ()):
                if edge.source_id in seen:
                    # Circular or cross-holding: the path would repeat a node, so it is
                    # not simple and contributes nothing. Terminating here is the whole
                    # reason a cycle cannot make this run forever.
                    continue
                if edge.source_id == owner_id:
                    # ``chain`` is already ordered owner-first (each recursion PREPENDS
                    # the deeper edge), so the rendered arithmetic reads the way a human
                    # walks it: from the ultimate owner down to the subject.
                    full = [edge, *chain]
                    found.append(self._path(tuple(_step(by_id, e) for e in full), full))
                    if len(found) >= self.max_paths:
                        return
                    continue
                walk(edge.source_id, [edge, *chain], seen | {edge.source_id})

        walk(graph.root_id, [], frozenset({graph.root_id}))
        return tuple(sorted(found, key=lambda p: (-p.product_pct, p.arithmetic)))

    def _path(
        self, steps: tuple[OwnershipPathStep, ...], edges: list[OwnershipEdge]
    ) -> OwnershipPath:
        product = 100.0
        for step in steps:
            product *= step.pct / 100.0
        product = _round(product, self.pct_decimals)
        arithmetic = (
            " x ".join(f"{s.pct:.2f}%" for s in steps) + f" = {product:.{self.pct_decimals}f}%"
        )
        return OwnershipPath(
            steps=steps,
            product_pct=product,
            arithmetic=arithmetic,
            citations=_dedupe([c for edge in edges for c in edge.citations]),
        )

    # ------------------------------------------------------------------ #
    # 3. The control ladder
    # ------------------------------------------------------------------ #
    def control(
        self, graph: OwnershipGraph, findings: Sequence[UboFinding]
    ) -> tuple[ControlBasis, str, tuple[UboFinding, ...]]:
        """Apply the explicit ladder and stamp the basis onto the findings that hold it.

        Tried in order and stopped at the first rung that holds, because the strongest
        available basis is the one a reviewer must see. The senior-managing-official rung
        is a FALLBACK: it is reached only when nobody owns or controls at the threshold,
        and it is recorded as such rather than dressed up as ownership.
        """
        by_id = {f.node_id: f for f in findings}
        stamped: dict[str, UboFinding] = {}

        holders = [
            f
            for f in findings
            if f.effective_pct >= self.control_threshold_pct and self._is_terminal(graph, f.node_id)
        ]
        if holders:
            for finding in holders:
                stamped[finding.node_id] = replace(
                    finding,
                    control_basis=ControlBasis.EFFECTIVE_OWNERSHIP,
                    control_reason=(
                        f"effective ownership {finding.effective_pct:.{self.pct_decimals}f}% "
                        f"is at or above the {self.control_threshold_pct:.2f}% control threshold"
                    ),
                )
            return (
                ControlBasis.EFFECTIVE_OWNERSHIP,
                self._rationale(ControlBasis.EFFECTIVE_OWNERSHIP, list(stamped.values())),
                self._merge(findings, stamped),
            )

        voting = self._direct_totals(graph, OwnershipEdgeKind.VOTING)
        voters = sorted(
            (nid for nid, pct in voting.items() if pct >= self.voting_threshold_pct),
        )
        if voters:
            for node_id in voters:
                base = by_id.get(node_id) or self._bare_finding(graph, node_id)
                stamped[node_id] = replace(
                    base,
                    control_basis=ControlBasis.VOTING_MAJORITY,
                    control_reason=(
                        f"holds {voting[node_id]:.2f}% of the voting rights recorded against "
                        f"{graph.root_name or graph.root_id}, at or above the "
                        f"{self.voting_threshold_pct:.2f}% threshold"
                    ),
                )
            return (
                ControlBasis.VOTING_MAJORITY,
                self._rationale(ControlBasis.VOTING_MAJORITY, list(stamped.values())),
                self._merge(findings, stamped),
            )

        seats = self._seat_counts(graph)
        total_seats = sum(seats.values())
        board = sorted(
            nid
            for nid, held in seats.items()
            if total_seats and held / total_seats >= self.board_majority_ratio
        )
        if board:
            for node_id in board:
                base = by_id.get(node_id) or self._bare_finding(graph, node_id)
                stamped[node_id] = replace(
                    base,
                    control_basis=ControlBasis.BOARD_MAJORITY,
                    control_reason=(
                        f"holds {seats[node_id]} of {total_seats} recorded board seat(s), at or "
                        f"above the {self.board_majority_ratio:.0%} board-majority threshold"
                    ),
                )
            return (
                ControlBasis.BOARD_MAJORITY,
                self._rationale(ControlBasis.BOARD_MAJORITY, list(stamped.values())),
                self._merge(findings, stamped),
            )

        contractual = sorted(
            {
                edge.source_id
                for edge in graph.edges
                if edge.kind is OwnershipEdgeKind.CONTRACTUAL and edge.target_id == graph.root_id
            }
        )
        if contractual:
            for node_id in contractual:
                base = by_id.get(node_id) or self._bare_finding(graph, node_id)
                stamped[node_id] = replace(
                    base,
                    control_basis=ControlBasis.CONTRACTUAL,
                    control_reason=(
                        "exercises control by agreement recorded against "
                        f"{graph.root_name or graph.root_id}"
                    ),
                )
            return (
                ControlBasis.CONTRACTUAL,
                self._rationale(ControlBasis.CONTRACTUAL, list(stamped.values())),
                self._merge(findings, stamped),
            )

        officials = sorted(seats)
        if officials:
            node_id = officials[0]
            base = by_id.get(node_id) or self._bare_finding(graph, node_id)
            stamped[node_id] = replace(
                base,
                control_basis=ControlBasis.SENIOR_MANAGING_OFFICIAL,
                control_reason=(
                    "no party reaches the ownership, voting, board or contractual control "
                    "threshold, so the senior managing official is recorded as the fallback "
                    "controller for verification"
                ),
            )
            return (
                ControlBasis.SENIOR_MANAGING_OFFICIAL,
                self._rationale(ControlBasis.SENIOR_MANAGING_OFFICIAL, list(stamped.values())),
                self._merge(findings, stamped),
            )

        return (
            ControlBasis.NONE,
            (
                "No party reaches any control rung and no senior managing official is "
                "recorded: the structure is unresolved and needs manual verification."
            ),
            tuple(findings),
        )

    @staticmethod
    def _rationale(basis: ControlBasis, findings: Sequence[UboFinding]) -> str:
        names = ", ".join(sorted(f.name for f in findings))
        label = basis.value.replace("_", " ")
        return f"Control established on the {label} rung of the ladder: {names}."

    @staticmethod
    def _merge(
        findings: Sequence[UboFinding], stamped: Mapping[str, UboFinding]
    ) -> tuple[UboFinding, ...]:
        merged = {f.node_id: f for f in findings}
        merged.update(stamped)
        return tuple(sorted(merged.values(), key=lambda f: (-f.effective_pct, f.node_id)))

    @staticmethod
    def _is_terminal(graph: OwnershipGraph, node_id: str) -> bool:
        """Can this party BE the answer, or is it a conduit the graph looks through?

        An intermediate holding company at 60% is not the beneficial owner: the whole
        purpose of walking the chain is to look THROUGH it to whoever sits above. So a
        body corporate qualifies for the control ladder only when nothing is recorded
        above it, in which case it is where the resolvable structure genuinely ends.
        Natural persons, trusts, nominees, state bodies and listed companies are terminal
        by their nature: a chain that reaches one of them has arrived somewhere.
        """
        node = graph.node_for(node_id)
        if node is None:
            return False
        if node.kind is not OwnershipNodeKind.ENTITY and node.kind is not OwnershipNodeKind.UNKNOWN:
            return True
        return not any(
            edge.target_id == node_id
            and edge.kind
            in (
                OwnershipEdgeKind.SHAREHOLDING,
                OwnershipEdgeKind.VOTING,
                OwnershipEdgeKind.NOMINEE_ARRANGEMENT,
            )
            for edge in graph.edges
        )

    def _bare_finding(self, graph: OwnershipGraph, node_id: str) -> UboFinding:
        """A finding for a controller that holds no equity (a director, a counterparty)."""
        node = graph.node_for(node_id)
        return UboFinding(
            node_id=node_id,
            name=node.name if node is not None else node_id,
            kind=node.kind if node is not None else OwnershipNodeKind.UNKNOWN,
            jurisdiction=node.jurisdiction if node is not None else "",
            is_pep=bool(node is not None and node.is_pep),
            citations=node.citations if node is not None else (),
        )

    def _direct_totals(self, graph: OwnershipGraph, kind: OwnershipEdgeKind) -> dict[str, float]:
        totals: dict[str, float] = {}
        for edge in graph.edges:
            if edge.kind is kind and edge.target_id == graph.root_id:
                totals[edge.source_id] = _round(
                    totals.get(edge.source_id, 0.0) + edge.pct, self.pct_decimals
                )
        return totals

    @staticmethod
    def _seat_counts(graph: OwnershipGraph) -> dict[str, int]:
        seats: dict[str, int] = {}
        for edge in graph.edges:
            if edge.kind is OwnershipEdgeKind.DIRECTORSHIP and edge.target_id == graph.root_id:
                seats[edge.source_id] = seats.get(edge.source_id, 0) + 1
        return seats

    # ------------------------------------------------------------------ #
    # 4. Nominee and shell INDICATORS (never conclusions)
    # ------------------------------------------------------------------ #
    def indicators(
        self, graph: OwnershipGraph, findings: Sequence[UboFinding]
    ) -> tuple[tuple[OwnershipFlag, ...], float]:
        """Raise the deterministic indicators and score the structure's opacity.

        Each of these is a REASON TO LOOK, not a finding of wrongdoing: nominee holdings,
        shells and secrecy jurisdictions are all lawful and common. They raise the opacity
        score, which drives the review severity; nothing here concludes anything.
        """
        flags: list[OwnershipFlag] = []
        flags.extend(self._structure_flags(graph))
        flags.extend(self._nominee_flags(graph))
        flags.extend(self._shell_flags(graph))
        if not any(f.meets_threshold for f in findings):
            flags.append(
                OwnershipFlag(
                    kind=OwnershipFlagKind.NO_OWNER_AT_THRESHOLD,
                    severity=Severity.MEDIUM,
                    summary=(
                        "No natural person reaches the "
                        f"{self.ownership_threshold_pct:.2f}% beneficial-ownership threshold."
                    ),
                    detail=(
                        "The structure discloses no owner at the policy threshold, so the "
                        "control ladder decided the outcome and a reviewer must verify it."
                    ),
                )
            )
        ordered = tuple(
            sorted(flags, key=lambda f: (_SEVERITY_ORDER[f.severity], f.kind.value, f.node_id))
        )
        # Scored per DISTINCT KIND, not per instance: six shell layers are one KIND of
        # opacity, and weighting per instance would saturate the score on any structure
        # of a realistic size, which destroys the very discrimination the score exists
        # for. How MANY parties carry an indicator is visible in the flags themselves.
        weight = sum(self.flag_weight.get(kind, 0.0) for kind in {f.kind.value for f in ordered})
        return ordered, _round(max(0.0, min(1.0, weight)), self.pct_decimals)

    def _structure_flags(self, graph: OwnershipGraph) -> list[OwnershipFlag]:
        flags: list[OwnershipFlag] = []
        for node_id in self._cycle_nodes(graph):
            node = graph.node_for(node_id)
            flags.append(
                OwnershipFlag(
                    kind=OwnershipFlagKind.CIRCULAR_HOLDING,
                    severity=Severity.HIGH,
                    node_id=node_id,
                    node_name=node.name if node is not None else node_id,
                    summary=(
                        f"{node.name if node is not None else node_id} sits in a circular "
                        "or cross-holding arrangement."
                    ),
                    detail=(
                        "Equity flows back into a layer already on the path, so the chain "
                        "does not terminate in a natural person by itself. Circular "
                        "holdings are lawful and common; this one needs a human to read."
                    ),
                    citations=node.citations if node is not None else (),
                )
            )
        if graph.truncated:
            flags.append(
                OwnershipFlag(
                    kind=OwnershipFlagKind.DEPTH_TRUNCATED,
                    severity=Severity.HIGH,
                    summary=(
                        f"The walk stopped at the policy limit (depth {self.max_depth}, "
                        f"{self.max_nodes} nodes): the structure is not fully resolved."
                    ),
                    detail=(
                        "Percentages below are computed over the layers that WERE resolved "
                        "and are therefore a floor, not the whole picture."
                    ),
                )
            )
        for node_id in graph.unresolved_ids:
            node = graph.node_for(node_id)
            flags.append(
                OwnershipFlag(
                    kind=OwnershipFlagKind.UNRESOLVED_LAYER,
                    severity=Severity.HIGH,
                    node_id=node_id,
                    node_name=node.name if node is not None else node_id,
                    summary=(
                        "The registry returned no ownership for "
                        f"{node.name if node is not None else node_id}."
                    ),
                    detail=(
                        "An opaque layer: whatever sits above it is invisible to this run, "
                        "so the effective percentages below understate the real picture."
                    ),
                )
            )
        for node in graph.nodes:
            risk = country_risk(node.jurisdiction, self.country_risk_policy)
            if risk.level in ("prohibited", "high"):
                flags.append(
                    OwnershipFlag(
                        kind=OwnershipFlagKind.SECRECY_JURISDICTION,
                        severity=(
                            Severity.CRITICAL if risk.level == "prohibited" else Severity.HIGH
                        ),
                        node_id=node.id,
                        node_name=node.name,
                        summary=(
                            f"{node.name} is registered in {risk.code}, a higher-risk "
                            "jurisdiction for beneficial-ownership transparency."
                        ),
                        detail=risk.reason,
                        citations=node.citations,
                    )
                )
        return flags

    @staticmethod
    def _cycle_nodes(graph: OwnershipGraph) -> tuple[str, ...]:
        """Node ids that lie on a directed equity cycle (Tarjan-free, small graphs)."""
        owns: dict[str, list[str]] = {}
        for edge in graph.edges:
            if edge.kind in _OWNERSHIP_EDGE_KINDS:
                owns.setdefault(edge.source_id, []).append(edge.target_id)
        on_cycle: set[str] = set()
        for start in sorted(owns):
            stack = [(start, frozenset({start}))]
            while stack:
                current, seen = stack.pop()
                for nxt in owns.get(current, ()):
                    if nxt == start:
                        on_cycle |= seen
                    elif nxt not in seen:
                        stack.append((nxt, seen | {nxt}))
        return tuple(sorted(on_cycle))

    def _nominee_flags(self, graph: OwnershipGraph) -> list[OwnershipFlag]:
        """Nominee signals: declared roles, recurring names, shared agent addresses."""
        flags: list[OwnershipFlag] = []
        declared = {
            edge.source_id
            for edge in graph.edges
            if edge.kind is OwnershipEdgeKind.NOMINEE_ARRANGEMENT
        }
        tokens = {t.casefold() for t in self.nominee_tokens}
        for node in graph.nodes:
            hits: list[str] = []
            if node.id in declared or node.kind is OwnershipNodeKind.NOMINEE:
                hits.append("the registry records the holding as a nominee arrangement")
            named = {t for t in normalize(node.name).split() if t in tokens}
            if named:
                hits.append(f"the recorded name carries the token(s) {', '.join(sorted(named))}")
            recurrence = self._recurrence(graph, node)
            if recurrence >= self.nominee_name_recurrence:
                hits.append(
                    f"the same name is recorded against {recurrence} otherwise unrelated "
                    "entities in this structure"
                )
            if not hits:
                continue
            flags.append(
                OwnershipFlag(
                    kind=OwnershipFlagKind.NOMINEE_INDICATOR,
                    severity=Severity.HIGH,
                    node_id=node.id,
                    node_name=node.name,
                    summary=f"{node.name} shows nominee characteristics.",
                    detail=(
                        "Indicator only, never a conclusion: nominee holdings are lawful. "
                        "Signals: " + "; ".join(hits) + "."
                    ),
                    citations=node.citations,
                )
            )
        for address, holders in self._shared_addresses(graph):
            flags.append(
                OwnershipFlag(
                    kind=OwnershipFlagKind.NOMINEE_INDICATOR,
                    severity=Severity.MEDIUM,
                    node_id=holders[0],
                    node_name=address,
                    summary=(
                        f"{len(holders)} entities share the registered address "
                        f"'{address}', which is characteristic of a registered agent."
                    ),
                    detail=(
                        "Indicator only: shared agent addresses are ordinary company-"
                        "formation practice. It matters here because it removes the "
                        "address as an independent corroboration of separateness."
                    ),
                )
            )
        return flags

    def _recurrence(self, graph: OwnershipGraph, node: OwnershipGraphNode) -> int:
        """How many distinct entities this party is recorded against (by fuzzy name).

        Uses the repo's EXISTING screening name matcher rather than a second, divergent
        notion of "the same person", so a party who reads as one entry to the sanctions
        engine reads as one party here too.
        """
        if node.kind is not OwnershipNodeKind.NATURAL_PERSON:
            return 0
        same = {
            other.id
            for other in graph.nodes
            if other.kind is OwnershipNodeKind.NATURAL_PERSON
            and name_score(node.name, other.name) >= self.nominee_name_threshold
        }
        return len({edge.target_id for edge in graph.edges if edge.source_id in same})

    def _shared_addresses(self, graph: OwnershipGraph) -> tuple[tuple[str, tuple[str, ...]], ...]:
        """(displayed address, node ids) for every address shared by enough entities."""
        by_address: dict[str, list[str]] = {}
        displayed: dict[str, str] = {}
        for node in graph.nodes:
            key = " ".join(node.registered_address.split()).casefold()
            if not key:
                continue
            by_address.setdefault(key, []).append(node.id)
            displayed.setdefault(key, " ".join(node.registered_address.split()))
        return tuple(
            (displayed[key], tuple(sorted(ids)))
            for key, ids in sorted(by_address.items())
            if len(ids) >= self.shared_address_entities
        )

    def _shell_flags(self, graph: OwnershipGraph) -> list[OwnershipFlag]:
        """Shell signals: single-owner pass-through layers, youth and dormancy.

        Raised only when a layer shows at least ``min_shell_signals`` of them together
        (secrecy jurisdiction is scored separately, through the shared country-risk
        module, so it is its own flag kind rather than a fourth signal here).
        """
        flags: list[OwnershipFlag] = []
        as_of = date.fromisoformat(graph.as_of) if graph.as_of else None
        dormant = {s.casefold() for s in self.dormant_statuses}
        for node in graph.nodes:
            if node.kind is OwnershipNodeKind.NATURAL_PERSON:
                continue
            hits: list[str] = []
            owners = [
                e for e in graph.edges if e.target_id == node.id and e.kind in _OWNERSHIP_EDGE_KINDS
            ]
            holdings = [
                e for e in graph.edges if e.source_id == node.id and e.kind in _OWNERSHIP_EDGE_KINDS
            ]
            if (
                node.id != graph.root_id
                and len(owners) == 1
                and len(holdings) == 1
                and owners[0].pct >= self.pass_through_pct
            ):
                hits.append(
                    f"a single owner holds {owners[0].pct:.2f}% and the layer holds exactly "
                    "one asset, so it passes value straight through"
                )
            if as_of is not None and node.incorporation_date:
                try:
                    incorporated = date.fromisoformat(node.incorporation_date[:10])
                except ValueError:
                    incorporated = None
                if incorporated is not None:
                    age = (as_of - incorporated).days
                    if 0 <= age < self.young_entity_days:
                        hits.append(f"incorporated {age} day(s) before the evaluation date")
            status = " ".join(node.status.split()).casefold()
            if status and any(token in status for token in dormant):
                hits.append(f"the registry filing status is '{node.status}'")
            if len(hits) < self.min_shell_signals:
                # One trait is not a pattern. A holding company with a single
                # shareholder that owns one asset is ordinary; the same company ALSO
                # newly formed and ALSO dormant is the thing worth a reviewer's minute.
                continue
            flags.append(
                OwnershipFlag(
                    kind=OwnershipFlagKind.SHELL_INDICATOR,
                    severity=Severity.MEDIUM,
                    node_id=node.id,
                    node_name=node.name,
                    summary=f"{node.name} shows shell-company characteristics.",
                    detail=(
                        "Indicator only, never a conclusion: holding companies with these "
                        "traits are ordinary. Signals: " + "; ".join(hits) + "."
                    ),
                    citations=node.citations,
                )
            )
        return flags

    # ------------------------------------------------------------------ #
    # 5. The whole resolution
    # ------------------------------------------------------------------ #
    def resolve(
        self,
        *,
        subject: Subject,
        as_of: date,
        fetch: HopFetcher,
        acl: tuple[str, ...] = (),
    ) -> UboResolution:
        """Traverse, compute, apply the ladder and flag, in one replayable step."""
        graph = self.traverse(subject=subject, as_of=as_of, fetch=fetch)
        findings = self.findings(graph)
        basis, rationale, findings = self.control(graph, findings)
        flags, opacity = self.indicators(graph, findings)
        owners = [f for f in findings if f.meets_threshold]
        summary = (
            f"UBO resolution for {subject.name} as at {as_of.isoformat()}: "
            f"{len(graph.nodes)} party(ies) across {len(graph.jurisdictions) or 1} "
            f"jurisdiction(s) to depth {graph.depth}"
            + (" (TRUNCATED)" if graph.truncated else "")
            + f"; {len(owners)} natural person(s) at or above "
            f"{self.ownership_threshold_pct:.2f}%; control basis "
            f"{basis.value.replace('_', ' ').upper()}; opacity {opacity:.4f} from "
            f"{len(flags)} indicator(s). Requires human review."
        )
        return UboResolution(
            subject_id=subject.id,
            subject_name=subject.name,
            tenant=subject.tenant,
            as_of=as_of.isoformat(),
            graph=graph,
            findings=findings,
            control_basis=basis,
            control_rationale=rationale,
            flags=flags,
            opacity_score=opacity,
            ownership_threshold_pct=self.ownership_threshold_pct,
            rationale=summary,
            requires_human_review=True,
            acl=acl,
            generated_at=_at(as_of),
        )

    def severity_for(self, opacity: float) -> Severity:
        """The review severity a given opacity score earns (deterministic banding).

        Bands are bank-owned policy (``UboGraphPolicy.opacity_severity_bands``): a score
        below every floor is LOW, so an adopter can retune the ladder in settings without a
        code change.
        """
        for floor, severity in self.opacity_severity_bands:
            if opacity >= floor:
                return Severity(severity)
        return Severity.LOW


_SEVERITY_ORDER: dict[Severity, int] = {
    Severity.CRITICAL: 0,
    Severity.HIGH: 1,
    Severity.MEDIUM: 2,
    Severity.LOW: 3,
}


def _step(by_id: Mapping[str, OwnershipGraphNode], edge: OwnershipEdge) -> OwnershipPathStep:
    source = by_id.get(edge.source_id)
    target = by_id.get(edge.target_id)
    return OwnershipPathStep(
        source_id=edge.source_id,
        target_id=edge.target_id,
        source_name=source.name if source is not None else edge.source_id,
        target_name=target.name if target is not None else edge.target_id,
        kind=edge.kind,
        pct=edge.pct,
    )


def _merge_node(
    node: OwnershipGraphNode, detail: OwnershipGraphNode, depth: int
) -> OwnershipGraphNode:
    """Enrich a node the walk already knew with what the registry just said about it.

    The subject enters the walk as a bare name; its jurisdiction, address, incorporation
    date and filing status arrive with its own hop. Registry detail wins over the
    placeholder, but never erases something already known with an empty string.
    """
    return replace(
        node,
        kind=detail.kind if detail.kind is not OwnershipNodeKind.UNKNOWN else node.kind,
        jurisdiction=detail.jurisdiction or node.jurisdiction,
        registered_address=detail.registered_address or node.registered_address,
        incorporation_date=detail.incorporation_date or node.incorporation_date,
        status=detail.status or node.status,
        is_pep=node.is_pep or detail.is_pep,
        depth=depth,
        citations=_dedupe([*node.citations, *detail.citations]),
    )


def to_ownership_summary(resolution: UboResolution) -> OwnershipSummary:
    """Convert a graph resolution DOWN to the flat :class:`OwnershipSummary`.

    The dossier (``CDDCase.ownership``) and the related-party derivation consume the flat
    one-hop shape, and this module is additive: rather than change either of them, the
    resolution converts. Only natural persons at or above the policy threshold become
    beneficial owners, which is exactly what the flat summary has always meant; the tree
    is rebuilt from the graph so the layers are still visible to anything that reads it.
    """
    graph = resolution.graph
    owners = tuple(
        BeneficialOwner(
            name=finding.name,
            pct=finding.effective_pct,
            country=finding.jurisdiction,
            is_pep=finding.is_pep,
            citations=finding.citations,
        )
        for finding in resolution.beneficial_owners
    )
    if graph is None:
        return OwnershipSummary(root_entity=resolution.subject_name, owners=owners)
    return OwnershipSummary(
        root_entity=resolution.subject_name or graph.root_name,
        owners=owners,
        tree=_tree(graph, graph.root_id, 100.0, frozenset({graph.root_id})),
        citations=graph.citations,
    )


def _tree(graph: OwnershipGraph, node_id: str, pct: float, seen: frozenset[str]) -> OwnershipNode:
    """The legacy nested tree, rebuilt from the graph with cycles cut at first repeat."""
    node = graph.node_for(node_id)
    children = tuple(
        _tree(graph, edge.source_id, edge.pct, seen | {edge.source_id})
        for edge in sorted(
            (
                e
                for e in graph.edges
                if e.target_id == node_id
                and e.kind in _OWNERSHIP_EDGE_KINDS
                and e.source_id not in seen
            ),
            key=lambda e: (-e.pct, e.source_id),
        )
    )
    return OwnershipNode(
        entity_name=node.name if node is not None else node_id, pct=pct, children=children
    )
