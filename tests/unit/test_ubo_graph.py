"""The UBO-graph engine: termination, path arithmetic, the ladder and the indicators.

Driven entirely from hand-built registry hops rather than an adapter, because the engine
is pure: given the same hops and the same ``as_of`` it must produce the same bytes, and a
test that cannot state the hops cannot state what the arithmetic should come to.

The invariants pinned here:

* a circular holding and a cross-holding TERMINATE, and the cycle is flagged rather than
  silently pruned;
* effective ownership is the sum over SIMPLE paths of the product of the shareholdings,
  so a diamond adds and a cycle does not double-count;
* the control ladder is tried in order and stops at the first rung that holds, with the
  senior-managing-official rung reachable only as a FALLBACK;
* an intermediate holding company is looked THROUGH, never reported as the controller;
* the nominee/shell signals are INDICATORS carrying their reasons; and
* every threshold is policy, so moving the beneficial-ownership percentage moves the
  answer without an engine edit.
"""

from __future__ import annotations

from datetime import date

import pytest

from cdd_sow_research.domain.models import (
    ControlBasis,
    OwnershipEdge,
    OwnershipEdgeKind,
    OwnershipFlagKind,
    OwnershipGraphNode,
    OwnershipNodeKind,
    RegistryHop,
    Severity,
    Subject,
    SubjectType,
)
from cdd_sow_research.domain.policy import CountryRiskPolicy, UboGraphPolicy
from cdd_sow_research.domain.ubo_graph import (
    UboGraphEngine,
    ownership_node_id,
    to_ownership_summary,
)

_AS_OF = date(2026, 8, 7)
_SUBJECT = Subject(
    id="sub",
    name="Subject Operating Co (FICTIONAL)",
    type=SubjectType.ENTITY,
    jurisdiction="SG",
    tenant="demo-bank",
)


# --------------------------------------------------------------------------- #
# A tiny hand-built registry: the hops ARE the test fixture.
# --------------------------------------------------------------------------- #
def _node(
    name: str,
    *,
    jurisdiction: str = "SG",
    kind: OwnershipNodeKind = OwnershipNodeKind.ENTITY,
    address: str = "",
    incorporated: str = "",
    status: str = "",
) -> OwnershipGraphNode:
    return OwnershipGraphNode(
        id=ownership_node_id(name, jurisdiction),
        name=name,
        kind=kind,
        jurisdiction=jurisdiction,
        registered_address=address,
        incorporation_date=incorporated,
        status=status,
    )


def _person(name: str, *, jurisdiction: str = "SG", address: str = "") -> OwnershipGraphNode:
    return _node(
        name, jurisdiction=jurisdiction, kind=OwnershipNodeKind.NATURAL_PERSON, address=address
    )


def _edge(
    owner: OwnershipGraphNode,
    owned: OwnershipGraphNode,
    pct: float = 0.0,
    kind: OwnershipEdgeKind = OwnershipEdgeKind.SHAREHOLDING,
) -> OwnershipEdge:
    return OwnershipEdge(source_id=owner.id, target_id=owned.id, kind=kind, pct=pct)


class _Registry:
    """Serves the declared hops and records what was asked for (termination proof)."""

    def __init__(self) -> None:
        self._hops: dict[str, RegistryHop] = {}
        self.asked: list[str] = []

    def declare(
        self,
        entity: OwnershipGraphNode,
        owners: tuple[OwnershipGraphNode, ...] = (),
        edges: tuple[OwnershipEdge, ...] = (),
        *,
        resolved: bool = True,
    ) -> _Registry:
        self._hops[entity.id] = RegistryHop(
            entity=entity, owners=owners, edges=edges, resolved=resolved
        )
        return self

    def hop(self, entity_name: str, jurisdiction: str) -> RegistryHop:
        node_id = ownership_node_id(entity_name, jurisdiction)
        self.asked.append(node_id)
        hop = self._hops.get(node_id)
        if hop is None:
            return RegistryHop(entity=_node(entity_name, jurisdiction=jurisdiction), resolved=False)
        return hop


def _engine(**overrides: object) -> UboGraphEngine:
    return UboGraphEngine.from_policy(
        UboGraphPolicy(**overrides),  # type: ignore[arg-type]
        CountryRiskPolicy(),
    )


def _subject_node() -> OwnershipGraphNode:
    return _node(_SUBJECT.name, jurisdiction=_SUBJECT.jurisdiction)


# --------------------------------------------------------------------------- #
# Effective ownership: the whole point of walking the chain
# --------------------------------------------------------------------------- #
def test_a_layered_chain_multiplies_into_one_effective_percentage():
    sub, holdco = _subject_node(), _node("Layer One Ltd (FICTIONAL)", jurisdiction="KY")
    person = _person("Ada Fenwick (FICTIONAL)", jurisdiction="KY")
    registry = (
        _Registry()
        .declare(sub, (holdco,), (_edge(holdco, sub, 50.0),))
        .declare(holdco, (person,), (_edge(person, holdco, 60.0),))
    )

    resolution = _engine().resolve(subject=_SUBJECT, as_of=_AS_OF, fetch=registry.hop)

    owners = {f.name: f for f in resolution.beneficial_owners}
    assert set(owners) == {"Ada Fenwick (FICTIONAL)"}
    # 60% of a company that holds 50% of the subject is 30% of the subject, not 60%.
    assert owners["Ada Fenwick (FICTIONAL)"].effective_pct == pytest.approx(30.0)
    path = owners["Ada Fenwick (FICTIONAL)"].paths[0]
    assert path.arithmetic == "60.00% x 50.00% = 30.0000%"
    assert [s.source_name for s in path.steps] == [
        "Ada Fenwick (FICTIONAL)",
        "Layer One Ltd (FICTIONAL)",
    ]


def test_a_diamond_sums_both_simple_paths():
    """Two routes to the same subject ADD; reading only the larger one understates."""
    sub = _subject_node()
    left = _node("Left Holdings Ltd (FICTIONAL)", jurisdiction="KY")
    right = _node("Right Holdings Ltd (FICTIONAL)", jurisdiction="JE")
    person = _person("Bram Oyelaran (FICTIONAL)")
    registry = (
        _Registry()
        .declare(sub, (left, right), (_edge(left, sub, 50.0), _edge(right, sub, 50.0)))
        .declare(left, (person,), (_edge(person, left, 30.0),))
        .declare(right, (person,), (_edge(person, right, 40.0),))
    )

    resolution = _engine().resolve(subject=_SUBJECT, as_of=_AS_OF, fetch=registry.hop)

    finding = next(f for f in resolution.findings if f.name == "Bram Oyelaran (FICTIONAL)")
    # 30% x 50% = 15%, plus 40% x 50% = 20%.
    assert finding.effective_pct == pytest.approx(35.0)
    assert len(finding.paths) == 2
    assert finding.meets_threshold


# --------------------------------------------------------------------------- #
# Termination: the reason a visited set exists
# --------------------------------------------------------------------------- #
def test_a_circular_holding_terminates_and_is_flagged():
    sub = _subject_node()
    alpha = _node("Circle Alpha Ltd (FICTIONAL)", jurisdiction="KY")
    beta = _node("Circle Beta Ltd (FICTIONAL)", jurisdiction="VG")
    registry = (
        _Registry()
        .declare(sub, (alpha,), (_edge(alpha, sub, 100.0),))
        .declare(alpha, (beta,), (_edge(beta, alpha, 100.0),))
        .declare(beta, (alpha,), (_edge(alpha, beta, 100.0),))
    )

    resolution = _engine().resolve(subject=_SUBJECT, as_of=_AS_OF, fetch=registry.hop)

    # Each entity is asked for exactly once: without the visited set this never returns.
    assert sorted(registry.asked) == sorted({sub.id, alpha.id, beta.id})
    assert OwnershipFlagKind.CIRCULAR_HOLDING.value in resolution.flag_kinds
    cycle = {f.node_id for f in resolution.flags if f.kind is OwnershipFlagKind.CIRCULAR_HOLDING}
    assert cycle == {alpha.id, beta.id}
    # A cycle can never terminate in a natural person, so nobody meets the threshold and
    # the engine says so rather than reporting a holding company as the owner.
    assert resolution.beneficial_owners == ()
    assert OwnershipFlagKind.NO_OWNER_AT_THRESHOLD.value in resolution.flag_kinds


def test_a_cross_holding_does_not_double_count_a_path_through_itself():
    """A path may not repeat a node, so the cycle contributes once, not endlessly."""
    sub = _subject_node()
    alpha = _node("Cross Alpha Ltd (FICTIONAL)", jurisdiction="KY")
    beta = _node("Cross Beta Ltd (FICTIONAL)", jurisdiction="VG")
    person = _person("Cleo Marchetti (FICTIONAL)", jurisdiction="KY")
    registry = (
        _Registry()
        .declare(sub, (alpha,), (_edge(alpha, sub, 100.0),))
        .declare(alpha, (beta, person), (_edge(beta, alpha, 50.0), _edge(person, alpha, 50.0)))
        .declare(beta, (alpha,), (_edge(alpha, beta, 100.0),))
    )

    resolution = _engine().resolve(subject=_SUBJECT, as_of=_AS_OF, fetch=registry.hop)

    finding = next(f for f in resolution.findings if f.name == "Cleo Marchetti (FICTIONAL)")
    assert finding.effective_pct == pytest.approx(50.0)
    assert len(finding.paths) == 1


def test_the_depth_limit_truncates_and_says_so():
    sub, holdco = _subject_node(), _node("Deep One Ltd (FICTIONAL)", jurisdiction="KY")
    person = _person("Dov Ellery (FICTIONAL)", jurisdiction="KY")
    registry = (
        _Registry()
        .declare(sub, (holdco,), (_edge(holdco, sub, 100.0),))
        .declare(holdco, (person,), (_edge(person, holdco, 100.0),))
    )

    resolution = _engine(max_depth=1).resolve(subject=_SUBJECT, as_of=_AS_OF, fetch=registry.hop)

    assert resolution.graph is not None
    assert resolution.graph.truncated is True
    assert OwnershipFlagKind.DEPTH_TRUNCATED.value in resolution.flag_kinds
    # The person one layer beyond the limit was never asked for, so is not claimed.
    assert person.id not in registry.asked
    assert resolution.beneficial_owners == ()


def test_an_unresolvable_layer_is_flagged_not_treated_as_transparent():
    sub, opaque = _subject_node(), _node("Opaque Ltd (FICTIONAL)", jurisdiction="VG")
    registry = _Registry().declare(sub, (opaque,), (_edge(opaque, sub, 100.0),))
    registry.declare(opaque, resolved=False)

    resolution = _engine().resolve(subject=_SUBJECT, as_of=_AS_OF, fetch=registry.hop)

    assert resolution.graph is not None
    assert resolution.graph.unresolved_ids == (opaque.id,)
    assert OwnershipFlagKind.UNRESOLVED_LAYER.value in resolution.flag_kinds


# --------------------------------------------------------------------------- #
# The control ladder, rung by rung
# --------------------------------------------------------------------------- #
def test_rung_one_effective_majority():
    sub = _subject_node()
    holdco = _node("Major Holdings Ltd (FICTIONAL)", jurisdiction="KY")
    person = _person("Fabio Reinholt (FICTIONAL)", jurisdiction="KY")
    registry = (
        _Registry()
        .declare(sub, (holdco,), (_edge(holdco, sub, 80.0),))
        .declare(holdco, (person,), (_edge(person, holdco, 100.0),))
    )

    resolution = _engine().resolve(subject=_SUBJECT, as_of=_AS_OF, fetch=registry.hop)

    assert resolution.control_basis is ControlBasis.EFFECTIVE_OWNERSHIP
    controllers = {f.name for f in resolution.controllers}
    # The person, NOT the 80% holding company the graph looks through.
    assert controllers == {"Fabio Reinholt (FICTIONAL)"}


def test_an_intermediate_holding_company_is_never_the_controller():
    """The whole purpose of the walk: a conduit at 80% is not the beneficial owner."""
    sub = _subject_node()
    holdco = _node("Conduit Ltd (FICTIONAL)", jurisdiction="KY")
    a = _person("Gwen Astorga (FICTIONAL)", jurisdiction="KY")
    b = _person("Hal Brantwood (FICTIONAL)", jurisdiction="KY")
    director = _person("Ivo Castellan (FICTIONAL)")
    registry = (
        _Registry()
        .declare(
            sub,
            (holdco, director),
            (
                _edge(holdco, sub, 80.0),
                _edge(director, sub, kind=OwnershipEdgeKind.DIRECTORSHIP),
            ),
        )
        .declare(holdco, (a, b), (_edge(a, holdco, 50.0), _edge(b, holdco, 50.0)))
    )

    resolution = _engine().resolve(subject=_SUBJECT, as_of=_AS_OF, fetch=registry.hop)

    # Neither person reaches 50% effective (each is 40%), and the conduit is skipped, so
    # the ladder falls through to the board.
    assert resolution.control_basis is ControlBasis.BOARD_MAJORITY
    assert {f.name for f in resolution.controllers} == {"Ivo Castellan (FICTIONAL)"}
    assert {f.name for f in resolution.beneficial_owners} == {
        "Gwen Astorga (FICTIONAL)",
        "Hal Brantwood (FICTIONAL)",
    }


def test_rung_two_voting_majority_when_votes_diverge_from_equity():
    sub = _subject_node()
    voter = _person("Jae Okonkwo (FICTIONAL)")
    registry = _Registry().declare(
        sub,
        (voter,),
        (
            _edge(voter, sub, 10.0),
            _edge(voter, sub, 60.0, kind=OwnershipEdgeKind.VOTING),
        ),
    )

    resolution = _engine().resolve(subject=_SUBJECT, as_of=_AS_OF, fetch=registry.hop)

    assert resolution.control_basis is ControlBasis.VOTING_MAJORITY
    controller = next(iter(resolution.controllers))
    assert controller.name == "Jae Okonkwo (FICTIONAL)"
    assert controller.effective_pct == pytest.approx(10.0)
    assert "60.00% of the voting rights" in controller.control_reason


def test_rung_three_board_majority():
    sub = _subject_node()
    chair = _person("Kira Lindqvist (FICTIONAL)")
    other = _person("Luc Moreau (FICTIONAL)")
    registry = _Registry().declare(
        sub,
        (chair, other),
        (
            _edge(chair, sub, kind=OwnershipEdgeKind.DIRECTORSHIP),
            _edge(chair, sub, 5.0),
            _edge(other, sub, 5.0),
        ),
    )

    resolution = _engine().resolve(subject=_SUBJECT, as_of=_AS_OF, fetch=registry.hop)

    assert resolution.control_basis is ControlBasis.BOARD_MAJORITY
    assert {f.name for f in resolution.controllers} == {"Kira Lindqvist (FICTIONAL)"}


def test_rung_four_contractual_control():
    sub = _subject_node()
    counterparty = _node("Agreement Partner Ltd (FICTIONAL)", jurisdiction="JE")
    minor = _person("Mina Vasquez (FICTIONAL)")
    registry = _Registry().declare(
        sub,
        (counterparty, minor),
        (
            _edge(counterparty, sub, kind=OwnershipEdgeKind.CONTRACTUAL),
            _edge(minor, sub, 20.0),
        ),
    )

    resolution = _engine().resolve(subject=_SUBJECT, as_of=_AS_OF, fetch=registry.hop)

    assert resolution.control_basis is ControlBasis.CONTRACTUAL
    assert {f.name for f in resolution.controllers} == {"Agreement Partner Ltd (FICTIONAL)"}


def test_the_senior_managing_official_is_the_fallback_when_no_rung_holds():
    """Three equal directors: no board majority, so the ladder falls to the SMO."""
    sub = _subject_node()
    directors = [_person(f"Director {n} (FICTIONAL)") for n in ("Alpha", "Beta", "Gamma")]
    registry = _Registry().declare(
        sub,
        tuple(directors),
        tuple(_edge(d, sub, kind=OwnershipEdgeKind.DIRECTORSHIP) for d in directors),
    )

    resolution = _engine().resolve(subject=_SUBJECT, as_of=_AS_OF, fetch=registry.hop)

    assert resolution.control_basis is ControlBasis.SENIOR_MANAGING_OFFICIAL
    controllers = list(resolution.controllers)
    assert len(controllers) == 1
    assert "no party reaches the ownership, voting, board or contractual control" in (
        controllers[0].control_reason
    )
    assert OwnershipFlagKind.NO_OWNER_AT_THRESHOLD.value in resolution.flag_kinds


def test_an_empty_structure_reaches_no_rung_at_all():
    registry = _Registry().declare(_subject_node())

    resolution = _engine().resolve(subject=_SUBJECT, as_of=_AS_OF, fetch=registry.hop)

    assert resolution.control_basis is ControlBasis.NONE
    assert resolution.findings == ()
    assert "unresolved" in resolution.control_rationale


# --------------------------------------------------------------------------- #
# Indicators: reasons to look, never conclusions
# --------------------------------------------------------------------------- #
def test_nominee_signals_name_token_recurrence_and_shared_address():
    address = "Suite 2, Agent House, Invented City (FICTIONAL)"
    sub = _subject_node()
    holder = _node("Quill Nominee Holdings Ltd (FICTIONAL)", jurisdiction="KY", address=address)
    sibling = _node("Sibling Ltd (FICTIONAL)", jurisdiction="KY", address=address)
    recurring = _person("Nils Ostergaard (FICTIONAL)", jurisdiction="KY")
    registry = (
        _Registry()
        .declare(
            sub,
            (holder, sibling, recurring),
            (
                _edge(holder, sub, 40.0),
                _edge(holder, sub, kind=OwnershipEdgeKind.NOMINEE_ARRANGEMENT),
                _edge(sibling, sub, 30.0),
                _edge(recurring, sub, kind=OwnershipEdgeKind.DIRECTORSHIP),
            ),
        )
        .declare(holder, (recurring,), (_edge(recurring, holder, 100.0),))
        .declare(sibling, (recurring,), (_edge(recurring, sibling, 100.0),))
    )

    resolution = _engine().resolve(subject=_SUBJECT, as_of=_AS_OF, fetch=registry.hop)

    nominee = [f for f in resolution.flags if f.kind is OwnershipFlagKind.NOMINEE_INDICATOR]
    reasons = " ".join(f.detail for f in nominee)
    assert "nominee arrangement" in reasons
    assert "token(s) nominee" in reasons
    assert "3 otherwise unrelated" in reasons
    assert any("share the registered address" in f.summary for f in nominee)
    # Every one of them is framed as an indicator, never as a conclusion.
    assert all("never a conclusion" in f.detail or "Indicator only" in f.detail for f in nominee)


def test_shell_signals_pass_through_youth_dormancy_and_secrecy_jurisdiction():
    sub = _subject_node()
    shell = _node(
        "Paper Layer Ltd (FICTIONAL)",
        jurisdiction="VE",  # a FATF increased-monitoring jurisdiction
        incorporated="2026-06-01",
        status="dormant",
    )
    person = _person("Ola Ferreira (FICTIONAL)", jurisdiction="VE")
    registry = (
        _Registry()
        .declare(sub, (shell,), (_edge(shell, sub, 100.0),))
        .declare(shell, (person,), (_edge(person, shell, 100.0),))
    )

    resolution = _engine().resolve(subject=_SUBJECT, as_of=_AS_OF, fetch=registry.hop)

    shell_flags = [f for f in resolution.flags if f.kind is OwnershipFlagKind.SHELL_INDICATOR]
    assert len(shell_flags) == 1
    detail = shell_flags[0].detail
    assert "passes value straight through" in detail
    assert "day(s) before the evaluation date" in detail
    assert "filing status is 'dormant'" in detail
    assert OwnershipFlagKind.SECRECY_JURISDICTION.value in resolution.flag_kinds
    assert resolution.opacity_score > 0.0


def test_the_opacity_score_is_bounded_and_policy_driven():
    sub = _subject_node()
    alpha = _node("Loop Alpha Ltd (FICTIONAL)", jurisdiction="KY")
    beta = _node("Loop Beta Ltd (FICTIONAL)", jurisdiction="VG")
    registry = (
        _Registry()
        .declare(sub, (alpha,), (_edge(alpha, sub, 100.0),))
        .declare(alpha, (beta,), (_edge(beta, alpha, 100.0),))
        .declare(beta, (alpha,), (_edge(alpha, beta, 100.0),))
    )

    scored = _engine().resolve(subject=_SUBJECT, as_of=_AS_OF, fetch=registry.hop)
    unweighted = _engine(flag_weight={}).resolve(subject=_SUBJECT, as_of=_AS_OF, fetch=registry.hop)

    assert 0.0 < scored.opacity_score <= 1.0
    # Same flags, different bank-owned weights: the score is policy, not a constant.
    assert unweighted.opacity_score == 0.0
    assert unweighted.flag_kinds == scored.flag_kinds


def test_the_review_severity_banding_is_bank_owned_policy():
    """severity_for reads the opacity->severity bands from policy, not a code constant."""
    default = _engine()
    assert default.severity_for(0.80) is Severity.CRITICAL
    assert default.severity_for(0.60) is Severity.HIGH
    assert default.severity_for(0.30) is Severity.MEDIUM
    assert default.severity_for(0.10) is Severity.LOW

    retuned = _engine(opacity_severity_bands=((0.90, "critical"), (0.40, "high")))
    assert retuned.severity_for(0.95) is Severity.CRITICAL
    # 0.30 is MEDIUM under the default ladder but LOW under this retuned one: the number
    # that decides is the bank's, so the routing severity moves without an engine edit.
    assert retuned.severity_for(0.30) is Severity.LOW


def test_the_review_payload_severity_and_dual_control_track_policy():
    """The Hrz7 payload bands severity and the four-eyes gate on the policy numbers too, so
    'every threshold, limit and weight is bank-owned policy' holds on the live routing path."""
    from cdd_sow_research.adapters._review_payload import resolution_to_review

    sub = _subject_node()
    holder = _node("Opaque Holdings Ltd (FICTIONAL)", jurisdiction="VE")  # secrecy => opacity
    person = _person("Ada Fenwick (FICTIONAL)", jurisdiction="SG")
    registry = (
        _Registry()
        .declare(sub, (holder,), (_edge(holder, sub, 100.0),))
        .declare(holder, (person,), (_edge(person, holder, 100.0),))
    )
    resolution = _engine().resolve(subject=_SUBJECT, as_of=_AS_OF, fetch=registry.hop)
    assert resolution.beneficial_owners  # an owner is found, so dual control hinges on opacity
    assert resolution.opacity_score > 0.0

    strict = UboGraphPolicy(opacity_severity_bands=((0.0, "critical"),), dual_control_opacity=0.0)
    lax = UboGraphPolicy(opacity_severity_bands=((1.0, "critical"),), dual_control_opacity=1.5)

    strict_review = resolution_to_review(resolution, maker="analyst@bank", policy=strict)
    lax_review = resolution_to_review(resolution, maker="analyst@bank", policy=lax)

    assert (strict_review.severity, strict_review.required_approvals) == ("critical", 2)
    assert (lax_review.severity, lax_review.required_approvals) == ("low", 1)


# --------------------------------------------------------------------------- #
# Policy, replayability and the conversion down to the flat summary
# --------------------------------------------------------------------------- #
def test_the_ownership_threshold_is_bank_owned():
    sub, holdco = _subject_node(), _node("Threshold Ltd (FICTIONAL)", jurisdiction="KY")
    person = _person("Pia Randall (FICTIONAL)", jurisdiction="KY")
    registry = (
        _Registry()
        .declare(sub, (holdco,), (_edge(holdco, sub, 40.0),))
        .declare(holdco, (person,), (_edge(person, holdco, 40.0),))
    )

    at_25 = _engine().resolve(subject=_SUBJECT, as_of=_AS_OF, fetch=registry.hop)
    at_10 = _engine(ownership_threshold_pct=10.0).resolve(
        subject=_SUBJECT, as_of=_AS_OF, fetch=registry.hop
    )

    # 40% x 40% = 16%: below a 25% threshold, above a 10% one. No engine edit either way.
    assert at_25.beneficial_owners == ()
    assert [f.name for f in at_10.beneficial_owners] == ["Pia Randall (FICTIONAL)"]
    assert at_10.ownership_threshold_pct == 10.0


def test_a_run_replays_byte_for_byte():
    sub, holdco = _subject_node(), _node("Replay Ltd (FICTIONAL)", jurisdiction="KY")
    person = _person("Quint Ashby (FICTIONAL)", jurisdiction="KY")
    registry = (
        _Registry()
        .declare(sub, (holdco,), (_edge(holdco, sub, 50.0),))
        .declare(holdco, (person,), (_edge(person, holdco, 60.0),))
    )
    engine = _engine()

    first = engine.resolve(subject=_SUBJECT, as_of=_AS_OF, fetch=registry.hop)
    second = engine.resolve(subject=_SUBJECT, as_of=_AS_OF, fetch=registry.hop)

    # Frozen dataclasses compare structurally, including the generated_at stamp, which is
    # derived from as_of rather than from a clock: that is what makes a replay exact.
    assert first == second
    assert first.generated_at == second.generated_at


def test_the_resolution_converts_down_to_the_flat_ownership_summary():
    sub, holdco = _subject_node(), _node("Convert Ltd (FICTIONAL)", jurisdiction="KY")
    person = _person("Rae Tolliver (FICTIONAL)", jurisdiction="KY")
    registry = (
        _Registry()
        .declare(sub, (holdco,), (_edge(holdco, sub, 60.0),))
        .declare(holdco, (person,), (_edge(person, holdco, 100.0),))
    )

    resolution = _engine().resolve(subject=_SUBJECT, as_of=_AS_OF, fetch=registry.hop)
    summary = to_ownership_summary(resolution)

    assert summary.root_entity == _SUBJECT.name
    assert [(o.name, o.pct) for o in summary.owners] == [("Rae Tolliver (FICTIONAL)", 60.0)]
    assert summary.tree is not None
    assert summary.tree.entity_name == _SUBJECT.name
    # The layers survive the conversion, so anything reading the old shape still sees them.
    assert [c.entity_name for c in summary.tree.children] == ["Convert Ltd (FICTIONAL)"]


def test_the_conversion_cuts_a_cycle_rather_than_recursing_forever():
    sub = _subject_node()
    alpha = _node("Tree Alpha Ltd (FICTIONAL)", jurisdiction="KY")
    beta = _node("Tree Beta Ltd (FICTIONAL)", jurisdiction="VG")
    registry = (
        _Registry()
        .declare(sub, (alpha,), (_edge(alpha, sub, 100.0),))
        .declare(alpha, (beta,), (_edge(beta, alpha, 100.0),))
        .declare(beta, (alpha,), (_edge(alpha, beta, 100.0),))
    )

    summary = to_ownership_summary(
        _engine().resolve(subject=_SUBJECT, as_of=_AS_OF, fetch=registry.hop)
    )

    assert summary.tree is not None
    node = summary.tree
    depth = 0
    while node.children:
        node = node.children[0]
        depth += 1
        assert depth < 10, "the rebuilt tree must terminate on a circular holding"
    assert depth == 2


def test_the_node_id_folds_registry_spelling_differences_into_one_party():
    """Two renderings of one company must be ONE node, or no cycle is ever detectable."""
    assert ownership_node_id("Acme Holdings Pte. Ltd.", "SG") == ownership_node_id(
        "ACME  HOLDINGS PTE LTD", "sg"
    )
    assert ownership_node_id("Acme Holdings", "SG") != ownership_node_id("Acme Holdings", "KY")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
