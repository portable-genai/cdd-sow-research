"""The A2A skill body must carry the owner list its frozen contract promises.

``to_jsonable`` walks ``dataclasses.fields``, and ``UboResolution.beneficial_owners`` is a
computed property, so the bare walk dropped it. ``docs/ubo-graph-contract.md`` freezes that
key on the ``resolve_ubo_graph`` body and its section 3 tells a consumer that ``findings``
is NOT the owner list and to read ``beneficial_owners`` instead. A consumer doing exactly
that read nothing back, and concluded no beneficial owner was identified for an entity
whose graph had resolved one above the threshold.

These tests pin the derived answer onto the serialized shape, and pin the boundary the fix
must not cross: the bare walk stays a pure field walk, because the managed case store
rehydrates through ``_dataclass_from_jsonable`` by field hints.
"""

from __future__ import annotations

from cdd_sow_research.domain.models import (
    ControlBasis,
    OwnershipNodeKind,
    UboFinding,
    UboResolution,
)
from cdd_sow_research.domain.serialization import to_jsonable, ubo_resolution_jsonable


def _person(name: str, pct: float, *, meets: bool) -> UboFinding:
    return UboFinding(
        node_id=f"{name.lower().replace(' ', '-')}@je",
        name=name,
        kind=OwnershipNodeKind.NATURAL_PERSON,
        jurisdiction="JE",
        effective_pct=pct,
        meets_threshold=meets,
    )


def _holdco(name: str, pct: float) -> UboFinding:
    """An intermediate the graph looks THROUGH: a candidate, never an owner."""
    return UboFinding(
        node_id=f"{name.lower().replace(' ', '-')}@ky",
        name=name,
        kind=OwnershipNodeKind.ENTITY,
        jurisdiction="KY",
        effective_pct=pct,
        meets_threshold=False,
    )


def _resolution() -> UboResolution:
    """One owner above the 25% threshold, behind a holdco and a below-threshold person."""
    return UboResolution(
        subject_id="acme",
        subject_name="Acme Holdings Pte Ltd (FICTIONAL)",
        tenant="demo-bank",
        as_of="2026-08-07",
        findings=(
            _holdco("Palewater Midco Ltd (FICTIONAL)", 75.0),
            _person("Ines Quiller (FICTIONAL)", 36.0, meets=True),
            _person("Tobias Renn (FICTIONAL)", 9.0, meets=False),
        ),
        control_basis=ControlBasis.BOARD_MAJORITY,
        ownership_threshold_pct=25.0,
    )


def test_bare_walk_drops_the_owner_list() -> None:
    """Characterizes the defect this wrapper exists to correct."""
    resolution = _resolution()
    assert [f.name for f in resolution.beneficial_owners] == ["Ines Quiller (FICTIONAL)"]

    assert "beneficial_owners" not in to_jsonable(resolution)


def test_skill_body_carries_the_owner_the_graph_resolved() -> None:
    body = ubo_resolution_jsonable(_resolution())

    assert [f["name"] for f in body["beneficial_owners"]] == ["Ines Quiller (FICTIONAL)"]
    assert body["beneficial_owners"][0]["effective_pct"] == 36.0
    assert body["beneficial_owners"][0]["meets_threshold"] is True


def test_skill_body_keeps_findings_wider_than_the_owner_list() -> None:
    """Contract section 3: ``findings`` is every candidate, not the owners."""
    body = ubo_resolution_jsonable(_resolution())

    assert len(body["findings"]) == 3
    assert len(body["beneficial_owners"]) == 1
    # The intermediate is looked through, never reported as an owner.
    names = {f["name"] for f in body["beneficial_owners"]}
    assert "Palewater Midco Ltd (FICTIONAL)" not in names


def test_skill_body_reports_no_owner_as_an_empty_list_not_a_missing_key() -> None:
    """A structure with nobody at the threshold must say so, not stay silent."""
    resolution = UboResolution(
        subject_id="opaque",
        findings=(_person("Tobias Renn (FICTIONAL)", 9.0, meets=False),),
    )
    body = ubo_resolution_jsonable(resolution)

    assert body["beneficial_owners"] == []
    assert len(body["findings"]) == 1


def test_skill_body_keeps_every_other_frozen_key() -> None:
    """The wrapper adds one key; it must not disturb the rest of the frozen shape."""
    resolution = _resolution()
    body = ubo_resolution_jsonable(resolution)

    assert set(to_jsonable(resolution)) <= set(body)
    assert body["subject_id"] == "acme"
    assert body["as_of"] == "2026-08-07"
    assert body["control_basis"] == "board_majority"
    assert body["ownership_threshold_pct"] == 25.0
    assert body["requires_human_review"] is True


def test_the_walk_itself_stays_a_pure_field_walk() -> None:
    """The rehydrator reconstructs by field hints, so no derived key may enter the walk.

    ``_dataclass_from_jsonable`` is keyed on ``dataclasses.fields``; the managed case store
    relies on ``to_jsonable(sow_case_from_jsonable(p)) == p``. A derived key in the walk
    would put a non-field into every persisted payload.
    """
    walked = to_jsonable(_resolution())

    assert "beneficial_owners" not in walked
    assert "controllers" not in walked
    assert "flag_kinds" not in walked
