"""The one reviewed decision about who a verified IAP caller is.

This module exists because the answer used to live twice. ``adapters/gcp/iap_identity.py`` is
the adapter ``config/settings.yaml`` binds, and its ``end_user_auth = VERIFIED`` declaration is
what stands the exposure guard down; ``api/security.py::get_authentication_port`` intercepted
``identity_mode == "iap"`` and returned a second implementation, so on the request path the
bound adapter's ``resolve`` was never called while its declaration still licensed the service.
The two disagreed about the subject, the tenant, the entitlement principals and whether the
forwarded portal header was read at all -- a divergence two files apart, recorded by
``tests/unit/test_iap_two_implementations.py`` rather than normalised away, because choosing
between them was a decision and not a cleanup.

The decision, taken 2026-08-26: the API-layer half wins, and it moves here so that both halves
are the same code rather than two implementations that happen to agree today. It is the
stricter of the two on every axis that carries authority:

- the subject is the immutable ``(iss, sub)`` pair, not the ``email`` claim, which a directory
  can reassign to a different person;
- the tenant comes from a reviewed domain map, and an unmapped domain REFUSES rather than
  passing the raw ``hd`` claim through as a partition key;
- the entitlement principals are stated, so a signed-in analyst holds the groups their domain
  was reviewed to hold instead of ``user:<subject>`` alone, which satisfies no case-access role.

The verification half did NOT move and did not need to: the adapter owns it, pins the algorithm
before any cryptography runs, checks the issuer that ``verify_token`` does not, and splits a
misconfigured deployment (503) from an unauthenticated caller (401).
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping

from .config import Settings
from .domain.identity import IdentityError, Principal

_ACTOR_PREFIX = "issub:"


def canonical_actor(issuer: str, source_subject: str) -> str:
    """Deterministically encode the immutable ``(iss, sub)`` pair as the audit actor."""
    pair = json.dumps([issuer, source_subject], separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(pair).decode("ascii").rstrip("=")
    return f"{_ACTOR_PREFIX}{encoded}"


def decode_canonical_actor(actor: str) -> tuple[str, str] | None:
    if not actor.startswith(_ACTOR_PREFIX):
        return None
    raw = actor.removeprefix(_ACTOR_PREFIX)
    try:
        padding = "=" * (-len(raw) % 4)
        value = json.loads(base64.urlsafe_b64decode(raw + padding))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(value, list)
        or len(value) != 2
        or not all(isinstance(item, str) and item for item in value)
    ):
        return None
    return value[0], value[1]


def _domain_for(claims: Mapping[str, object]) -> str:
    """The domain a reviewed map is keyed by.

    ``hd`` is a Google hosted domain and is absent entirely on a machine identity, so a service
    account's email domain is the closest thing it has to one; naming that domain in the map is
    how a deployment admits a machine caller.
    """
    hosted_domain = str(claims.get("hd") or "").strip().lower()
    email = str(claims.get("email") or "").strip().lower()
    return hosted_domain or email.rpartition("@")[2]


def reviewed_groups_for(claims: Mapping[str, object], settings: Settings) -> tuple[str, ...]:
    """The reviewed group principals a VERIFIED identity domain holds here.

    The groups are the same strings the entitlement rules already recognise; naming a domain
    states who holds them, and grants nothing that was not already grantable.
    """
    return tuple(settings.identity.iap_groups_by_domain.get(_domain_for(claims), ()))


def reviewed_tenant_for(claims: Mapping[str, object], settings: Settings) -> str:
    """Resolve the reviewed tenant for a VERIFIED assertion, or the empty string.

    With no map configured this returns ``hd`` exactly as it did before the map existed, so an
    existing deployment is unchanged. With one configured the map is exhaustive: an unmapped
    domain resolves to nothing and the caller is refused by ``reviewed_principal_from_iap_claims``.
    """
    hosted_domain = str(claims.get("hd") or "").strip().lower()
    mapping = settings.identity.iap_tenant_by_domain
    if not mapping:
        return hosted_domain
    return str(mapping.get(_domain_for(claims), "")).strip()


def reviewed_principal_from_iap_claims(
    claims: Mapping[str, object],
    settings: Settings,
    *,
    expected_issuer: str,
) -> tuple[Principal, str, str]:
    """Map verified IAP claims onto the reviewed principal, issuer and source subject.

    The issuer and source subject are returned alongside the principal because the audit
    evidence records them as presented, while the principal's subject is the encoded pair.
    """
    issuer = str(claims.get("iss") or "").strip()
    source_subject = str(claims.get("sub") or "").strip()
    if issuer != expected_issuer or not source_subject:
        raise IdentityError("IAP assertion is missing the exact issuer/sub identity")
    tenant = reviewed_tenant_for(claims, settings)
    if not tenant:
        raise IdentityError("IAP assertion did not resolve a policy-mapped tenant")
    subject = canonical_actor(issuer, source_subject)
    principal = Principal(
        subject=subject,
        principals=(f"user:{subject}", *reviewed_groups_for(claims, settings)),
        tenant=tenant,
        assurance="iap",
        source="gcp-iap",
    )
    return principal, issuer, source_subject
