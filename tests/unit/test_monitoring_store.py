"""The MonitoringStorePort contract: fail-closed ACL, queue ordering, on-prem fail-fast.

The store is where perpetual KYC's object-level authorization lives, so these tests pin
the rules rather than the implementation: a subset ACL on the baseline, a tenant-tag match
on the listing (with an untagged record never listed), a deterministic queue order, and an
on-prem placeholder that raises instead of quietly answering "no baseline, empty queue".
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from cdd_sow_research.adapters.local.monitoring_store import LocalMonitoringStoreAdapter
from cdd_sow_research.adapters.onprem.monitoring_store import OnPremMonitoringStoreAdapter
from cdd_sow_research.config import Settings
from cdd_sow_research.domain.entitlements import case_tags
from cdd_sow_research.domain.errors import CaseAccessDeniedError
from cdd_sow_research.domain.models import QueuePriority, Subject, SubjectType
from cdd_sow_research.domain.perpetual_kyc import PerpetualKycEngine
from cdd_sow_research.domain.serialization import (
    perpetual_kyc_assessment_from_jsonable,
    perpetual_kyc_baseline_from_jsonable,
    to_jsonable,
)

_ENGINE = PerpetualKycEngine()


def _assessment(subject_id: str, tenant: str, priority: QueuePriority = QueuePriority.STANDARD):
    subject = Subject(
        id=subject_id,
        name=f"{subject_id.title()} Holdings Pte Ltd (FICTIONAL)",
        type=SubjectType.ENTITY,
        tenant=tenant,
    )
    assessment = _ENGINE.assess(
        subject=subject, as_of=date(2026, 8, 5), acl=case_tags(subject_id, tenant)
    )
    assert assessment.queue_item is not None
    return replace(
        assessment,
        queue_item=replace(assessment.queue_item, priority=priority, sla_due="2026-09-01"),
    )


def _store() -> LocalMonitoringStoreAdapter:
    return LocalMonitoringStoreAdapter(Settings())


def test_a_baseline_is_refused_to_a_caller_missing_a_tag():
    store = _store()
    assessment = _assessment("acme", "demo-bank")
    store.save_baseline(_ENGINE.next_baseline(assessment))

    owner = ("case:acme", "tenant:demo-bank")
    assert store.load_baseline("acme", owner) is not None

    with pytest.raises(CaseAccessDeniedError):
        store.load_baseline("acme", ("case:acme", "tenant:other-bank"))


def test_an_absent_baseline_is_none_not_an_error():
    assert _store().load_baseline("never-seen", ("tenant:demo-bank",)) is None


def test_the_queue_is_tenant_scoped_and_fails_closed_without_a_tag():
    store = _store()
    store.record(_assessment("acme", "demo-bank"))
    store.record(_assessment("beta", "other-bank"))
    store.record(replace(_assessment("gamma", "demo-bank"), acl=()))  # untagged

    mine = store.queue(("group:cdd-analyst", "tenant:demo-bank"))
    assert [a.subject_id for a in mine] == ["acme"]
    assert store.queue(("group:cdd-analyst",)) == ()


def test_the_queue_is_ordered_most_urgent_first():
    store = _store()
    store.record(_assessment("low-one", "demo-bank", QueuePriority.LOW))
    store.record(_assessment("urgent-one", "demo-bank", QueuePriority.URGENT))
    store.record(_assessment("high-one", "demo-bank", QueuePriority.HIGH))

    order = [a.subject_id for a in store.queue(("tenant:demo-bank",))]
    assert order == ["urgent-one", "high-one", "low-one"]


def test_a_re_run_supersedes_the_previous_queue_entry():
    store = _store()
    store.record(_assessment("acme", "demo-bank", QueuePriority.LOW))
    store.record(_assessment("acme", "demo-bank", QueuePriority.URGENT))
    queued = store.queue(("tenant:demo-bank",))
    assert len(queued) == 1
    assert queued[0].queue_item is not None
    assert queued[0].queue_item.priority is QueuePriority.URGENT


def test_the_managed_store_round_trips_the_assessment_graph():
    """The Firestore adapter stores ``to_jsonable`` and reads back domain objects."""
    assessment = _assessment("acme", "demo-bank", QueuePriority.URGENT)
    baseline = _ENGINE.next_baseline(assessment)

    assert perpetual_kyc_assessment_from_jsonable(to_jsonable(assessment)) == assessment
    assert perpetual_kyc_baseline_from_jsonable(to_jsonable(baseline)) == baseline


def test_the_onprem_placeholder_fails_fast_on_every_method():
    adapter = OnPremMonitoringStoreAdapter(Settings())
    assessment = _assessment("acme", "demo-bank")
    with pytest.raises(NotImplementedError):
        adapter.load_baseline("acme", ("tenant:demo-bank",))
    with pytest.raises(NotImplementedError):
        adapter.save_baseline(_ENGINE.next_baseline(assessment))
    with pytest.raises(NotImplementedError):
        adapter.record(assessment)
    with pytest.raises(NotImplementedError):
        adapter.queue(("tenant:demo-bank",))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
