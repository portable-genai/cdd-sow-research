"""Object-level authorization: server-side case entitlements + tenant isolation.

Covers the Phase B1 security fix: the ``case:<id>`` retrieval principal is derived
server-side (``domain/entitlements.py``), evidence is tagged with case AND tenant, and
the knowledge-base ACL match is subset-based and fail-closed, so an authenticated user
in another tenant gets ZERO passages for a case id they merely guessed.
"""

from __future__ import annotations

import pytest

from cdd_sow_research.adapters.local.knowledge_base import LocalKnowledgeBaseAdapter
from cdd_sow_research.config import LocalSettings, Settings
from cdd_sow_research.domain import entitlements
from cdd_sow_research.domain.errors import CaseAccessDeniedError
from cdd_sow_research.domain.identity import Principal
from cdd_sow_research.domain.models import DocType, KycDocument, RetrievalQuery

ANALYST = Principal(
    subject="demo.analyst@bank.example",
    principals=("group:cdd-analyst", "group:risk"),
    tenant="demo-bank",
)
OTHER_TENANT = Principal(
    subject="user@other-tenant.example",
    principals=("group:cdd-analyst",),
    tenant="other-bank",
)
NO_ROLES = Principal(subject="viewer@bank.example", principals=("group:hr",), tenant="demo-bank")
EXPLICIT_GRANT = Principal(
    subject="temp@bank.example", principals=("case:acme",), tenant="demo-bank"
)


# --------------------------------------------------------------------------- #
# Entitlement rules
# --------------------------------------------------------------------------- #
def test_case_access_roles_and_explicit_grants() -> None:
    assert entitlements.may_access_case(ANALYST, "acme") is True
    assert entitlements.may_access_case(EXPLICIT_GRANT, "acme") is True
    assert entitlements.may_access_case(EXPLICIT_GRANT, "zeta") is False
    assert entitlements.may_access_case(NO_ROLES, "acme") is False


def test_case_scope_denies_unentitled_principal() -> None:
    with pytest.raises(CaseAccessDeniedError):
        entitlements.case_scope(NO_ROLES, "acme")


def test_case_scope_contains_tenant_and_case_principals() -> None:
    scope = entitlements.case_scope(ANALYST, "acme")
    assert "case:acme" in scope
    assert "tenant:demo-bank" in scope
    assert set(ANALYST.principals) <= set(scope)


# --------------------------------------------------------------------------- #
# Tenant isolation end-to-end through the local KB (subset ACL, fail-closed)
# --------------------------------------------------------------------------- #
def _kb() -> LocalKnowledgeBaseAdapter:
    settings = Settings(local=LocalSettings(db_path=":memory:", audit_path=":memory:"))
    kb = LocalKnowledgeBaseAdapter(settings)
    kb.ingest(
        KycDocument(id="acme-reg", doc_type=DocType.REGISTRY_EXTRACT),
        b"Acme Holdings Pte Ltd (FICTIONAL) registered in Singapore, UEN 202612345Z.",
        acl_tags=("case:acme", "tenant:demo-bank"),
    )
    return kb


def test_other_tenant_analyst_gets_zero_passages_for_guessed_case_id() -> None:
    kb = _kb()
    scope = entitlements.case_scope(OTHER_TENANT, "acme")  # role passes, tenant differs
    hits = kb.search(RetrievalQuery(text="Acme Holdings", acl_principals=scope, top_k=5))
    assert hits == []


def test_same_tenant_analyst_sees_the_case_evidence() -> None:
    kb = _kb()
    scope = entitlements.case_scope(ANALYST, "acme")
    hits = kb.search(RetrievalQuery(text="Acme Holdings", acl_principals=scope, top_k=5))
    assert hits and all("case:acme" in p.acl_tags for p in hits)


def test_acl_is_fail_closed_for_empty_principals() -> None:
    kb = _kb()
    hits = kb.search(RetrievalQuery(text="Acme Holdings", acl_principals=(), top_k=5))
    assert hits == []


def test_untagged_passages_stay_public_reference_data() -> None:
    settings = Settings(local=LocalSettings(db_path=":memory:", audit_path=":memory:"))
    kb = LocalKnowledgeBaseAdapter(settings)
    kb.ingest(
        KycDocument(id="ref-1", doc_type=DocType.OTHER),
        b"FATF guidance on source of wealth corroboration (public reference).",
        acl_tags=(),
    )
    hits = kb.search(RetrievalQuery(text="FATF guidance", acl_principals=(), top_k=5))
    assert hits
