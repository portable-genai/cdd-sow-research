"""Server-side case entitlements: who may read a case's evidence, decided here.

The retrieval ACL for a case is NEVER derived from a client-supplied identifier alone.
A request names a case (subject id), but the ``case:<id>`` retrieval principal is
granted only after :func:`may_access_case` passes against the VERIFIED
:class:`~cdd_sow_research.domain.identity.Principal` (resolved by the IdentityPort, never
client-asserted). Combined with tenant tagging at ingest (see
``CddService._acl_tags``) and the subset ACL match in the knowledge-base adapters, this
closes the object-level authorization gap: an authenticated user cannot read another
tenant's case evidence by guessing its subject id.

Access model (deliberately simple, override per deployment):

* an explicit ``case:<id>`` entitlement on the principal always grants access
  (fine-grained grants provisioned by the IdP / entitlement system); otherwise
* membership of one of the ``case_access_roles`` grants access to cases, and tenant
  isolation is still enforced by the ``tenant:<tenant>`` tag at retrieval time.

Pure stdlib; raising :class:`CaseAccessDeniedError` maps to HTTP 403 at the API layer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .errors import CaseAccessDeniedError
from .identity import Principal

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .models import SowCase


def case_tags(case_id: str, tenant: str = "") -> tuple[str, ...]:
    """The ACL tags a case's artifacts carry: ``case:<id>``, plus ``tenant:<t>``.

    The single definition of "what a case's evidence is tagged with", shared by the
    dossier pipeline, the case store and the document store, so custody, indexing and
    retrieval cannot drift apart into differently-scoped ACLs.
    """
    if tenant:
        return (f"case:{case_id}", f"tenant:{tenant}")
    return (f"case:{case_id}",)


def case_acl(case: SowCase) -> tuple[str, ...]:
    """The ACL tags a case carries in the case store (mirrors ``CddService._acl_tags``).

    Always ``case:<id>``; plus ``tenant:<t>`` when the subject belongs to a tenant. The
    store's subset match then requires a reader to hold every tag, so a case id alone
    never crosses a tenant boundary (object-level authorization).
    """
    return case_tags(case.id, case.subject.tenant)


def case_acl_ok(case: SowCase, principals: tuple[str, ...]) -> bool:
    """Subset, fail-closed: the caller must hold every one of the case's ACL tags."""
    return set(case_acl(case)) <= set(principals)


#: Roles whose members may work on cases (within their own tenant). Deployments with
#: finer-grained needs provision explicit ``case:<id>`` entitlements instead.
DEFAULT_CASE_ACCESS_ROLES: frozenset[str] = frozenset(
    {"group:cdd-analyst", "group:cdd-approver", "group:audit"}
)


def may_access_case(
    principal: Principal,
    case_id: str,
    roles: frozenset[str] = DEFAULT_CASE_ACCESS_ROLES,
) -> bool:
    """True when the verified principal is entitled to the case's evidence."""
    if f"case:{case_id}" in principal.principals:
        return True
    return any(p in roles for p in principal.principals)


def queue_scope(
    principal: Principal,
    roles: frozenset[str] = DEFAULT_CASE_ACCESS_ROLES,
) -> tuple[str, ...]:
    """The ACL principals for a *listing* (perpetual-KYC review queue), server-derived.

    A listing names no single case, so the ``case:<id>`` grant cannot be the gate. The
    caller must instead hold a case-access role AND a tenant; the returned scope carries
    that tenant tag, and the store lists only records whose tenant tags are a subset of
    it. Fail-closed in both directions: no role or no tenant means no queue at all, and a
    record with no tenant tag is never listed.
    """
    if not any(p in roles for p in principal.principals):
        raise CaseAccessDeniedError(
            f"{principal.actor} holds no case-access role, so the review queue is empty"
        )
    if not principal.tenant:
        raise CaseAccessDeniedError(
            f"{principal.actor} carries no tenant, so no tenant-scoped queue can be granted"
        )
    return (*principal.principals, f"tenant:{principal.tenant}")


def queue_acl_ok(acl: tuple[str, ...], principals: tuple[str, ...]) -> bool:
    """Fail-closed tenant match for a listed record: every tenant tag must be held."""
    tenant_tags = {tag for tag in acl if tag.startswith("tenant:")}
    if not tenant_tags:
        return False
    return tenant_tags <= set(principals)


def case_scope(
    principal: Principal,
    case_id: str,
    roles: frozenset[str] = DEFAULT_CASE_ACCESS_ROLES,
) -> tuple[str, ...]:
    """The retrieval ACL principals for ``case_id``, derived entirely server-side.

    Returns the principal's own entitlements plus ``tenant:<tenant>`` (when the
    principal carries a tenant) plus ``case:<case_id>``; raises
    :class:`CaseAccessDeniedError` when the principal is not entitled to the case.
    The knowledge-base ACL match is subset-based, so a passage tagged with another
    tenant stays invisible even though the ``case:<id>`` principal is present.
    """
    if not may_access_case(principal, case_id, roles):
        raise CaseAccessDeniedError(
            f"{principal.actor} is not entitled to case {case_id!r} "
            "(no explicit case grant and no case-access role)"
        )
    scope: list[str] = list(principal.principals)
    if principal.tenant:
        scope.append(f"tenant:{principal.tenant}")
    scope.append(f"case:{case_id}")
    return tuple(scope)
