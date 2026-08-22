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
