"""SowCase serialization round-trip + case-store ACL enforcement.

Guards the managed-case-store contract: the deep
SowCase graph round-trips through the open JSON format the Firestore adapter stores, and
the local store enforces the same subset ACL so cross-tenant isolation is real (not
vacuous). The Firestore adapter shares this ACL + serialization, exercised live by the
@integration smoke test.
"""

from __future__ import annotations

import pytest

from cdd_sow_research.adapters.local.case_store import LocalCaseStoreAdapter
from cdd_sow_research.config import Settings
from cdd_sow_research.domain.entitlements import case_acl
from cdd_sow_research.domain.errors import CaseAccessDeniedError
from cdd_sow_research.domain.models import Subject, SubjectType
from cdd_sow_research.domain.serialization import sow_case_from_jsonable, to_jsonable
from cdd_sow_research.domain.sow_case_service import SowCaseService


def _rich_case():
    """A deep, realistic SowCase built through the real service (declaration + analysis)."""
    store = LocalCaseStoreAdapter(Settings())
    svc = SowCaseService(store=store)
    subject = Subject(
        id="acme",
        name="Acme Holdings Pte Ltd (FICTIONAL)",
        type=SubjectType.ENTITY,
        jurisdiction="SG",
        tenant="tenant-b",
    )
    svc.open("acme", subject, None, actor="rm@bank.test")
    return store.load("acme", ("case:acme", "tenant:tenant-b"))


# --------------------------------------------------------------------------- #
# Serialization round trip
# --------------------------------------------------------------------------- #
def test_sow_case_round_trips_through_the_open_json_format():
    case = _rich_case()
    payload = to_jsonable(case)
    restored = sow_case_from_jsonable(payload)
    # Object equality (frozen dataclass graph) and a stable re-serialization.
    assert restored == case
    assert to_jsonable(restored) == payload
    # Enums, tz-aware datetimes and the tenant survive.
    assert restored.subject.type is SubjectType.ENTITY
    assert restored.subject.tenant == "tenant-b"
    assert restored.opened_at.tzinfo is not None


# --------------------------------------------------------------------------- #
# ACL enforcement (mirrors the Firestore adapter's fail-closed subset match)
# --------------------------------------------------------------------------- #
def test_case_acl_includes_tenant_when_present():
    case = _rich_case()
    assert set(case_acl(case)) == {"case:acme", "tenant:tenant-b"}


def test_same_tenant_caller_loads_the_case():
    store = LocalCaseStoreAdapter(Settings())
    SowCaseService(store=store).open(
        "acme", Subject(id="acme", name="Acme (FICTIONAL)", tenant="tenant-b"), None, actor="rm"
    )
    got = store.load("acme", ("case:acme", "tenant:tenant-b"))
    assert got.id == "acme"


def test_cross_tenant_caller_gets_403_on_load():
    store = LocalCaseStoreAdapter(Settings())
    SowCaseService(store=store).open(
        "acme", Subject(id="acme", name="Acme (FICTIONAL)", tenant="tenant-b"), None, actor="rm"
    )
    # A caller in tenant-a holds case:acme (the requested id) but not tenant:tenant-b.
    with pytest.raises(CaseAccessDeniedError):
        store.load("acme", ("case:acme", "tenant:tenant-a", "group:cdd-analyst"))


def test_cross_tenant_caller_sees_zero_from_list_for():
    store = LocalCaseStoreAdapter(Settings())
    svc = SowCaseService(store=store)
    svc.open(
        "acme", Subject(id="acme", name="Acme (FICTIONAL)", tenant="tenant-b"), None, actor="rm"
    )
    svc.open(
        "beta", Subject(id="beta", name="Beta (FICTIONAL)", tenant="tenant-a"), None, actor="rm"
    )
    # A tenant-a caller listing sees only tenant-a cases.
    visible = store.list_for(("tenant:tenant-a", "case:acme", "case:beta", "group:cdd-analyst"))
    assert {c.id for c in visible} == {"beta"}


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
