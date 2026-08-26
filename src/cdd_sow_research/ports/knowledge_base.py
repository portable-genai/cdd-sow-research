"""KnowledgeBaseClientPort — the case's governed RAG store (A2 Enterprise KB).

B1 does **not** build its own retrieval backend: the case's KYC documents are ingested
into the shared **A2 Enterprise Knowledge Base** with case ACL tags and retrieved from
it (rule R3, governed RAG). The ``platform`` adapter is a thin HTTP client to A2's
``/v1/ingest`` and ``/v1/search`` (env ``HRZ_KB_URL``); the on-prem placeholder stub
raises, and a direct GCP adapter (Agent Search) is available for standalone runs.

ACL contract (every adapter must enforce it, fail-closed):

* Ingested passages carry the ``acl_tags`` given at ingest, normally ``case:<id>`` plus
  ``tenant:<tenant>`` (see ``CddService._acl_tags``).
* A tagged passage is returned only when the query's ``acl_principals`` contain EVERY
  tag on the passage (subset / all-of semantics). Any-overlap matching is a security
  bug: it lets a ``case:<id>`` guessed by an authenticated user cross tenants.
* An untagged passage is public reference data and always visible; an empty
  ``acl_principals`` sees only untagged passages (never everything).
* ``acl_principals`` must be derived from the server-side verified Principal
  (``domain/entitlements.py``), never from a request-body field.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..domain.models import IngestResult, KycDocument, RetrievalQuery, RetrievedPassage


@runtime_checkable
class KnowledgeBaseClientPort(Protocol):
    def ingest(
        self,
        document: KycDocument,
        content: bytes,
        acl_tags: tuple[str, ...],
        page_texts: tuple[str, ...] = (),
    ) -> IngestResult:
        """Index a case KYC document into the governed RAG store with ACL tags.

        ``page_texts`` is the same content already split by source page (page N is
        ``page_texts[N - 1]``), as recovered by the extractor. When supplied, an adapter
        indexes one passage per page so a retrieved claim cites the page it came from;
        when omitted, the whole document is one passage. It is optional because not
        every extractor can recover page boundaries, not because page-level provenance
        is optional: a dossier citation that cannot be checked is not evidence.
        """
        ...

    def search(self, query: RetrievalQuery) -> list[RetrievedPassage]:
        """Retrieve ranked passages (ACL-filtered) for grounding the dossier."""
        ...

    def retract(self, document_id: str, acl_principals: tuple[str, ...]) -> bool:
        """Remove every indexed passage belonging to ``document_id``. Returns whether any went.

        This port carried ``ingest`` and ``search`` and nothing else until 2026-08-26, which
        meant **evidence could enter retrieval and never leave it**. The custody store has always
        had ``delete``, so removing a document there left its passages indexed and citable: the
        two halves disagreed, and retrieval -- the half a dossier actually quotes -- kept the
        copy. That is how 21 pre-fix duplicate copies of one bank statement survived a repair
        that reported success, and it is why the paired demonstration's citation counts kept
        diverging on history rather than on any disagreement between the profiles.

        It is more than a duplicate-cleanup gap. A CDD system that cannot retract an indexed
        document cannot honour an erasure request, cannot withdraw evidence filed against the
        wrong case, and cannot correct a document it later learns is forged -- while continuing
        to cite all three.

        ACL principals are required and checked for the same reason they are on every read: a
        retraction is a write against evidence, and a caller who cannot read a document must not
        be able to remove it.
        """
        ...
