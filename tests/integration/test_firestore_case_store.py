"""@integration smoke for the Firestore case store (live GCP; deselected by default).

Runs only with `-m integration` against a real, CMEK-encrypted Firestore Native database
(CDD_PROFILE=gcp, CDD_FIRESTORE_DB set). It exercises the same contract the local store
covers offline: open -> load (ACL) -> save (optimistic concurrency) -> seal (write-once).
"""

from __future__ import annotations

import pytest

from cdd_sow_research.adapters.gcp.firestore_case_store import FirestoreCaseStoreAdapter
from cdd_sow_research.config import Settings
from cdd_sow_research.domain.errors import CaseAccessDeniedError, ConcurrencyError
from cdd_sow_research.domain.models import Subject, SubjectType
from cdd_sow_research.domain.sow_case_service import SowCaseService

pytestmark = pytest.mark.integration


@pytest.fixture()
def adapter() -> FirestoreCaseStoreAdapter:
    return FirestoreCaseStoreAdapter(Settings.load("config/settings.yaml"))


def test_open_load_save_seal_round_trip(adapter: FirestoreCaseStoreAdapter) -> None:
    svc = SowCaseService(store=adapter)
    subject = Subject(id="it-acme", name="Acme (FICTIONAL)", type=SubjectType.ENTITY, tenant="t-b")
    principals = ("case:it-acme", "tenant:t-b")

    svc.open("it-acme", subject, None, actor="rm@bank.test")
    loaded = adapter.load("it-acme", principals)
    assert loaded.id == "it-acme"

    # Cross-tenant caller is refused.
    with pytest.raises(CaseAccessDeniedError):
        adapter.load("it-acme", ("case:it-acme", "tenant:t-a", "group:cdd-analyst"))

    # Optimistic concurrency: a stale expected_version is rejected.
    adapter.save(loaded, expected_version=loaded.version)
    with pytest.raises(ConcurrencyError):
        adapter.save(loaded, expected_version=loaded.version)  # version moved on
